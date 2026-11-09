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

调度器（开发文档 §7）。

Phase 1：单账号即可跑通完整链路；多账号并发结构一次到位，Phase 2 直接启用。
职责：时间窗口过滤、CC 模式 /me/* 自动过滤（§5.4）、到期判定、
并发信号量、调用后状态更新与下一轮随机间隔、status.json 落盘。
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx
from datetime import timedelta

from .account import AccountState
from .api_executor import GraphExecutor, RoundResult
from .auth import AuthProvider
from .models import ApiDefinition, AppConfig, AuthType
from .utils import in_schedule, now_in_tz, rand_interval

logger = logging.getLogger("e5pulse")

LOOP_INTERVAL_SECONDS = 45
_ME_PREFIX = "https://graph.microsoft.com/v1.0/me"


class Scheduler:
    def __init__(
        self,
        config: AppConfig,
        stop_event: asyncio.Event,
        transport: httpx.BaseTransport | None = None,
        loop_interval: int = LOOP_INTERVAL_SECONDS,
    ) -> None:
        self._cfg = config
        self._stop = stop_event
        self._transport = transport
        self._loop_interval = loop_interval
        self._states = {a.name: AccountState(config=a) for a in config.accounts}
        self._sem = asyncio.Semaphore(config.global_.max_concurrent_accounts)
        # 每账号一把执行锁：手动触发与调度循环可能同时看上同一账号
        self._run_locks: dict[str, asyncio.Lock] = {
            a.name: asyncio.Lock() for a in config.accounts
        }

    def apply_config(self, config: AppConfig) -> None:
        """热改后应用新配置（§12.1）：替换配置，保留已有运行态统计。

        仅限可热改项变化时调用；凭证/账号增删类需重启，不走这里。
        新出现的账号补状态，被移除的账号清状态。
        """
        old_states = self._states
        new_states = {}
        for account in config.accounts:
            if account.name in old_states:
                state = old_states[account.name]
                state.config = account  # 刷新账号级配置引用
                new_states[account.name] = state
            else:
                new_states[account.name] = AccountState(config=account)
        self._states = new_states
        self._cfg = config
        self._sem = asyncio.Semaphore(config.global_.max_concurrent_accounts)
        for name in config.accounts:
            self._run_locks.setdefault(name.name, asyncio.Lock())
        logger.info("新配置已热应用到调度器（运行态统计保留）")

    # ---------- §5.4 认证模式 API 过滤 ----------
    def effective_apis(self, account_name: str) -> list[ApiDefinition]:
        account = next(a for a in self._cfg.accounts if a.name == account_name)
        apis = account.effective_apis(self._cfg.default_apis)
        if account.auth_type is AuthType.CLIENT_CREDENTIALS:
            filtered = [
                api for api in apis
                if not (api.enabled and api.url.startswith(_ME_PREFIX))
            ]
            dropped = len([a for a in apis if a.enabled]) - len(
                [a for a in filtered if a.enabled]
            )
            if dropped:
                logger.warning(
                    "[%s] no user context in CC mode, auto-filtered %d /me/* API(s) (sec 5.4)",
                    account_name, dropped,
                )
        else:
            filtered = apis
        effective = [a for a in filtered if a.enabled]
        logger.info(
            "[%s] 实际生效 API %d 个: %s",
            account_name, len(effective),
            ", ".join(a.name for a in effective) or "(空!)",
        )
        return effective

    async def run_once(self) -> dict[str, RoundResult]:
        """跑一轮所有到期账号（Phase 1 验证 / 面板手动触发复用）。"""
        now = now_in_tz(self._cfg.global_.timezone)
        tasks = []
        for account in self._cfg.accounts:
            if not account.enabled:
                continue
            state = self._states[account.name]
            if state.is_paused(now):
                logger.info("[%s] 暂停中，跳过（至 %s）",
                            account.name, state.pause_until or "手动恢复")
                continue
            schedule = account.effective_schedule(self._cfg.schedule)
            if not in_schedule(now, schedule):
                logger.debug("[%s] 不在工作时间窗口，跳过", account.name)
                continue
            if not state.is_due(now):
                logger.debug("[%s] 未到下次执行时间 %s，跳过",
                             account.name, state.next_run_at)
                continue
            tasks.append(self._run_account(account.name))

        if not tasks:
            logger.info("本轮无到期账号")
            return {}
        results = await asyncio.gather(*tasks)
        self._write_status()
        return dict(results)

    async def run_forever(self) -> None:
        logger.info("调度主循环启动，巡检间隔 %ds", self._loop_interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("调度轮次异常，已兜底不退出主循环")
            if await self._wait(self._loop_interval):
                break
        self._write_status()
        logger.info("调度主循环已停止")

    async def _wait(self, seconds: int) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_account(self, name: str) -> tuple[str, RoundResult]:
        # 账号级锁：同一账号的调度执行与手动触发互斥，避免并发重复调用 Graph
        async with self._run_locks[name]:
            async with self._sem:
                account = next(a for a in self._cfg.accounts if a.name == name)
                state = self._states[name]
                apis = self.effective_apis(name)
                auth = AuthProvider(account)
                executor = GraphExecutor(
                    auth, name, apis, self._cfg.call_policy,
                    self._cfg.error_handling,
                    self._cfg.global_.request_timeout, self._stop,
                    transport=self._transport,
                )
                try:
                    result = await executor.run_round()
                finally:
                    await executor.aclose()

                now = now_in_tz(self._cfg.global_.timezone)
                reason = state.record_result(
                    result.success, now, self._cfg.error_handling, result.error
                )
                lo, hi = account.effective_interval(self._cfg.global_)
                state.schedule_next(
                    now + timedelta(seconds=rand_interval(lo, hi))
                )
                if reason:
                    logger.error("[%s] 触发智能暂停: %s（暂停 %d 分钟）",
                                 name, reason, self._cfg.error_handling.pause_minutes)
                elif result.fatal:
                    # 凭证类致命错误：立即冷却，避免疯狂打微软接口
                    state.trigger_pause(
                        now, self._cfg.error_handling,
                        f"致命错误: {result.error}",
                    )
                    logger.error("[%s] 致命错误，立即暂停: %s", name, result.error)
                return name, result

    # ---------- 面板操作（Phase 4-②）----------

    def _require_account(self, name: str):
        if name not in self._states:
            raise KeyError(name)
        return self._states[name]

    async def trigger_now(self, name: str) -> dict:
        """手动触发某账号立即执行一轮；绕过时间窗口与到期判断，但不绕过暂停。"""
        state = self._require_account(name)
        now = now_in_tz(self._cfg.global_.timezone)
        if state.is_paused(now):
            return {"ok": False, "reason": "Account is paused, resume it first",
                    "pause_until": state.pause_until.isoformat()
                    if state.pause_until else None}
        if self._run_locks[name].locked():
            return {"ok": False, "reason": "Account is already running, do not trigger again"}
        logger.info("[%s] 面板手动触发执行", name)
        _, result = await self._run_account(name)
        self._write_status()
        return {
            "ok": result.success,
            "fatal": result.fatal,
            "error": result.error,
            "calls": [
                {"api": c.api_name, "success": c.success,
                 "status": c.status_code, "elapsed_ms": c.elapsed_ms}
                for c in result.calls
            ],
        }

    def pause_account(self, name: str) -> dict:
        """运行态手动暂停（重启复位，与配置级 enabled 独立，§12.1）。"""
        state = self._require_account(name)
        state.set_manual_pause(True)
        logger.info("[%s] 面板手动暂停", name)
        self._write_status()
        return {"ok": True, "manual_pause": True}

    def resume_account(self, name: str) -> dict:
        """恢复：解除手动暂停，并清掉错误冷却与连续失败计数。"""
        state = self._require_account(name)
        state.set_manual_pause(False)
        state.pause_until = None
        state.consecutive_failures = 0
        logger.info("[%s] 面板手动恢复，已清除冷却状态", name)
        self._write_status()
        return {"ok": True, "manual_pause": False}

    def _write_status(self) -> None:
        path = Path(self._cfg.global_.state_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._status_generated_at = now_in_tz(
                self._cfg.global_.timezone
            ).isoformat()
            payload = {
                "generated_at": self._status_generated_at,
                "accounts": [s.snapshot() for s in self._states.values()],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.exception("failed to write status.json (main flow unaffected)")

    def status_generated_at(self) -> str | None:
        return getattr(self, "_status_generated_at", None)

    @property
    def states(self) -> dict[str, AccountState]:
        return self._states
