from __future__ import annotations

"""
E5Pulse - self-hosted Microsoft 365 E5 keep-alive service.
Copyright (C) 2026 TheMrxk

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Graph API 调用执行器（开发文档 §5、§8）。

- 每轮随机抽 1~N 个 API、随机顺序（§5.2）
- 可重试错误指数退避（§8.2），429 优先尊重 Retry-After
- 401/403 刷新 token 后整轮重试一次；凭证类错误立即终止
- 输出结构化 RoundResult 给账号状态机
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from .auth import AuthConfigError, AuthProvider, AuthTransientError
from .models import ApiDefinition, CallPolicy, ErrorHandlingConfig, HttpMethod
from .utils import interruptible_sleep, sample_apis

logger = logging.getLogger("e5pulse")

USER_AGENT = "E5Pulse/0.1 (self-hosted; Microsoft-Graph-keepalive)"
_RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class CallResult:
    api_name: str
    success: bool
    status_code: int | None
    elapsed_ms: int
    error: str | None = None
    retry_after: float | None = None


@dataclass
class RoundResult:
    success: bool
    calls: list[CallResult] = field(default_factory=list)
    fatal: bool = False          # 凭证/配置类错误，应直接暂停
    error: str | None = None


class GraphExecutor:
    def __init__(
        self,
        auth: AuthProvider,
        account_name: str,
        apis: list[ApiDefinition],
        policy: CallPolicy,
        err_cfg: ErrorHandlingConfig,
        timeout: int,
        stop_event: asyncio.Event,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._name = account_name
        self._apis = apis
        self._policy = policy
        self._err_cfg = err_cfg
        self._timeout = timeout
        self._stop = stop_event
        # transport 注入点：生产为 None 走真实网络，测试注入 MockTransport
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run_round(self) -> RoundResult:
        picked = sample_apis(
            self._apis,
            self._policy.apis_per_round_min,
            self._policy.apis_per_round_max,
            self._policy.shuffle_order,
        )
        if not picked:
            return RoundResult(success=False, fatal=True, error="没有可用的启用 API")

        try:
            token = await self._auth.get_token()
        except AuthConfigError as exc:
            return RoundResult(success=False, fatal=True, error=str(exc))
        except AuthTransientError as exc:
            return RoundResult(success=False, error=str(exc))

        results: list[CallResult] = []
        for idx, api in enumerate(picked):
            if self._stop.is_set():
                break
            cr = await self._call_with_retry(api, token)
            results.append(cr)
            logger.info(
                "[%s] %s %s -> %s (%dms)%s",
                self._name, api.method.value, api.name,
                "OK" if cr.success else "FAIL", cr.elapsed_ms,
                f" {cr.error}" if cr.error else "",
            )
            # 401/403：强制刷新 token 后重试一次（§8.1）
            if cr.status_code in (401, 403) and not cr.success:
                self._auth.invalidate()
                try:
                    token = await self._auth.get_token()
                    cr = await self._call_once(api, token)
                    results[-1] = cr
                    logger.info(
                        "[%s] 刷新 token 后重试 %s -> %s",
                        self._name, api.name, "OK" if cr.success else "FAIL",
                    )
                except AuthConfigError as exc:
                    return RoundResult(success=False, fatal=True, error=str(exc), calls=results)
                except AuthTransientError as exc:
                    return RoundResult(success=False, error=str(exc), calls=results)

            if not cr.success and cr.status_code not in _RETRY_STATUS | {401, 403}:
                # 确定性业务错误（如 400/404），整轮判失败但不继续浪费配额
                return RoundResult(
                    success=False,
                    calls=results,
                    fatal=cr.status_code in (400,),
                    error=f"{api.name} 确定性错误: {cr.error}",
                )

            if idx < len(picked) - 1:
                await interruptible_sleep(
                    self._rand_inter_delay(), self._stop
                )

        ok = all(c.success for c in results)
        return RoundResult(
            success=ok,
            calls=results,
            error=None if ok else f"{sum(not c.success for c in results)} 个 API 调用失败",
        )

    def _rand_inter_delay(self) -> float:
        lo, hi = self._policy.inter_call_delay_min, self._policy.inter_call_delay_max
        return float(lo) if lo == hi else float(lo) + (hi - lo) * (time.time() % 1)

    async def _call_with_retry(self, api: ApiDefinition, token: str) -> CallResult:
        last: CallResult | None = None
        for attempt in range(self._err_cfg.retry_times + 1):
            cr = await self._call_once(api, token)
            if cr.success or (cr.status_code is not None and cr.status_code not in _RETRY_STATUS):
                return cr
            last = cr
            if attempt < self._err_cfg.retry_times:
                delay = self._retry_delay(cr, attempt)
                logger.warning(
                    "[%s] %s 第 %d 次失败，%.1fs 后重试: %s",
                    self._name, api.name, attempt + 1, delay, cr.error,
                )
                if await interruptible_sleep(delay, self._stop):
                    break
        return last or CallResult(api.name, False, None, 0, "重试耗尽")

    def _retry_delay(self, cr: CallResult, attempt: int) -> float:
        # 429 的 Retry-After 优先（§8.2），状态对象里透传在 error 字段不优雅，
        # 因此由 _call_once 把 retry_after 挂到 CallResult 上
        if cr.retry_after is not None:
            return float(cr.retry_after)
        if attempt < len(self._err_cfg.retry_backoff):
            return float(self._err_cfg.retry_backoff[attempt])
        return float(self._err_cfg.retry_backoff[-1])

    async def _call_once(self, api: ApiDefinition, token: str) -> CallResult:
        start = time.monotonic()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        }
        if api.method is not HttpMethod.GET:
            headers["Content-Type"] = "application/json"
        try:
            resp = await self._client.request(
                api.method.value, api.url, headers=headers, json=api.body
            )
            elapsed = int((time.monotonic() - start) * 1000)
            retry_after = resp.headers.get("Retry-After")
            ok = resp.is_success
            err = None if ok else f"HTTP {resp.status_code}: {resp.text[:150]}"
            return CallResult(
                api.name, ok, resp.status_code, elapsed, err,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        except httpx.TimeoutException:
            return CallResult(api.name, False, None,
                              int((time.monotonic() - start) * 1000), "请求超时")
        except httpx.HTTPError as exc:
            return CallResult(api.name, False, None,
                              int((time.monotonic() - start) * 1000), f"网络错误: {exc}")
