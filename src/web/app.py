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

Embedded FastAPI panel: holds the Scheduler reference in-process and reads
in-memory state without IPC. /api/auth/login, /health and static assets are
public; every other /api/* route requires a valid signed cookie.
"""

import logging
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config_store import ConfigStore, HotConfigError
from ..auth import AuthError, AuthProvider
from ..models import AppConfig, AccountConfig, AuthType
from ..scheduler import Scheduler
from .auth_session import COOKIE_NAME, SessionAuth

logger = logging.getLogger("e5pulse")


class LoginBody(BaseModel):
    password: str


class AccountEnabledBody(BaseModel):
    enabled: bool


class IntervalBody(BaseModel):
    min_interval: int
    max_interval: int


class ApiEnabledBody(BaseModel):
    api_name: str
    enabled: bool
    account_name: str | None = None


class ScheduleBody(BaseModel):
    enabled: bool | None = None
    workdays: list[int] | None = None
    time_windows: list[dict] | None = None


class ConcurrencyBody(BaseModel):
    max_concurrent: int = Field(ge=1, le=20)


class TimeoutBody(BaseModel):
    timeout: int = Field(ge=5, le=120)


class ErrorHandlingBody(BaseModel):
    max_consecutive_failures: int = Field(ge=1)
    pause_minutes: int = Field(ge=1)
    max_error_rate: float = Field(gt=0.0, le=1.0)
    sliding_window_size: int = Field(ge=2)
    retry_times: int = Field(ge=0, le=6)
    retry_backoff: list[int]


class CallPolicyBody(BaseModel):
    apis_per_round_min: int = Field(ge=1)
    apis_per_round_max: int = Field(ge=1)
    shuffle_order: bool = True
    inter_call_delay_min: int = Field(ge=0)
    inter_call_delay_max: int = Field(ge=0)


class NewAccountBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    min_interval: int | None = Field(default=None, ge=30)
    max_interval: int | None = Field(default=None, ge=30)


class DeleteAccountBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def create_app(
    config: AppConfig,
    scheduler: Scheduler,
    config_path: str,
) -> FastAPI:
    panel = config.panel
    app = FastAPI(title="E5Pulse 面板", docs_url=None, redoc_url=None, openapi_url=None)
    store = ConfigStore(config_path)

    if not panel.password:
        raise ValueError("panel.password is required when panel.enabled=true")

    auth = SessionAuth(
        password=panel.password.get_secret_value(),
        session_secret=panel.session_secret.get_secret_value()
        if panel.session_secret else None,
        fail_limit=panel.login_fail_limit,
    )

    def client_ip(request: Request) -> str:
        # 简单取直连 IP；若走 Cloudflare 隧道，真实 IP 在 CF-Connecting-IP
        return request.headers.get("CF-Connecting-IP") or (
            request.client.host if request.client else "unknown"
        )

    def require_login(request: Request) -> None:
        token = request.cookies.get(COOKIE_NAME)
        if not auth.validate_cookie(token):
            raise HTTPException(status_code=401, detail="Unauthorized or session expired")

    # ---------------- 鉴权 ----------------

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        ip = client_ip(request)
        locked, wait = auth.is_locked(ip)
        if locked:
            raise HTTPException(
                status_code=429, detail=f"Too many failed attempts, try again in {wait} seconds"
            )
        if not auth.verify_password(ip, body.password):
            raise HTTPException(status_code=401, detail="Incorrect password")
        response.set_cookie(
            COOKIE_NAME, auth.issue_cookie(),
            max_age=12 * 3600, httponly=True, samesite="lax", path="/",
        )
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/auth/check")
    def auth_check(request: Request):
        return {"authenticated": auth.validate_cookie(request.cookies.get(COOKIE_NAME))}

    # ---------------- 健康检查（无需登录，供 Docker/监控） ----------------

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "e5pulse"}

    # ---------------- 只读状态 ----------------

    @app.get("/api/status", dependencies=[Depends(require_login)])
    def status():
        return {
            "generated_at": scheduler.status_generated_at(),
            "accounts": [s.snapshot() for s in scheduler.states.values()],
        }

    @app.get("/api/config/overview", dependencies=[Depends(require_login)])
    def config_overview():
        """脱敏后的配置概览（绝不回传 secret）。

        统一从 scheduler._cfg 读，热改后该引用已被 apply_config 刷新；
        不能用闭包里的初始 config，否则热改后回显旧值。
        """
        live = scheduler._cfg
        g = live.global_
        return {
            "timezone": g.timezone,
            "max_concurrent_accounts": g.max_concurrent_accounts,
            "request_timeout": g.request_timeout,
            "default_interval": [g.default_min_interval, g.default_max_interval],
            "schedule": live.schedule.model_dump(),
            "error_handling": live.error_handling.model_dump(),
            "call_policy": live.call_policy.model_dump(),
            "default_apis": [
                {
                    "name": api.name,
                    "method": api.method,
                    "url": api.url,
                    "enabled": api.enabled,
                }
                for api in live.default_apis
            ],
            "accounts": [
                {
                    "name": a.name,
                    "enabled": a.enabled,
                    "auth_type": a.auth_type.value,
                    "tenant_id": a.tenant_id,
                    "interval": list(a.effective_interval(g)),
                    "effective_apis": [
                        api.name for api in scheduler.effective_apis(a.name)
                    ],
                }
                for a in live.accounts
            ],
        }

    @app.get("/api/logs", dependencies=[Depends(require_login)])
    def tail_logs(lines: int = 200):
        """读日志文件尾部，默认 200 行。限制最大 2000 防大响应。"""
        lines = max(1, min(lines, 2000))
        log_path = Path(scheduler._cfg.global_.log_file)
        if not log_path.exists():
            return {"lines": []}
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                tail = deque(fh, maxlen=lines)
        except OSError as exc:
            raise HTTPException(500, f"Failed to read logs: {exc}") from exc
        return {"lines": [line.rstrip("\n") for line in tail]}

    # ---------------- 操作类（Phase 4-②，均需登录 + 写审计日志） ----------------

    @app.post("/api/accounts/{name}/trigger", dependencies=[Depends(require_login)])
    async def trigger_account(name: str, request: Request):
        try:
            result = await scheduler.trigger_now(name)
        except KeyError:
            raise HTTPException(404, f"Account not found: {name}")
        logger.info("面板操作 trigger account=%s ip=%s ok=%s",
                    name, client_ip(request), result.get("ok"))
        if not result.get("ok") and "already running" in str(result.get("reason", "")).lower():
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/accounts/{name}/pause", dependencies=[Depends(require_login)])
    def pause_account(name: str, request: Request):
        try:
            result = scheduler.pause_account(name)
        except KeyError:
            raise HTTPException(404, f"Account not found: {name}")
        logger.info("面板操作 pause account=%s ip=%s", name, client_ip(request))
        return result

    @app.post("/api/accounts/{name}/resume", dependencies=[Depends(require_login)])
    def resume_account(name: str, request: Request):
        try:
            result = scheduler.resume_account(name)
        except KeyError:
            raise HTTPException(404, f"Account not found: {name}")
        logger.info("面板操作 resume account=%s ip=%s", name, client_ip(request))
        return result

    # ---------------- 账号增删（Phase 4-⑥，保存前真实校验凭证） ----------------

    @app.post("/api/accounts", dependencies=[Depends(require_login)])
    async def add_account(body: NewAccountBody, request: Request):
        # 1) 先用凭证真实换一次 token，凭证错误直接拒绝、绝不写盘
        probe = AccountConfig.model_validate({
            "name": body.name,
            "enabled": True,
            "auth_type": AuthType.CLIENT_CREDENTIALS,
            "tenant_id": body.tenant_id,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "min_interval": body.min_interval,
            "max_interval": body.max_interval,
        })
        provider = AuthProvider(probe)
        try:
            await provider.get_token()
        except AuthError as exc:
            logger.warning("新增账号凭证验证失败 name=%s ip=%s err=%s",
                           body.name, client_ip(request), exc)
            raise HTTPException(400, f"Credential validation failed, nothing saved: {exc}")
        except Exception as exc:
            # MSAL 构造期权威发现失败（假 tenant GUID/网络问题）也在此拦截，
            # 统一转 400，避免 500 且绝不写盘
            logger.warning("新增账号验证异常 name=%s ip=%s err=%s",
                           body.name, client_ip(request), exc)
            raise HTTPException(400, f"Credential validation failed, nothing saved: {exc}")
        # 2) 凭证有效，再落盘（重名/重复/Pydantic 校验在 store 内拦截）
        try:
            new_cfg = store.add_account(body.model_dump())
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        scheduler.apply_config(new_cfg)
        logger.info("面板新增账号 account=%s ip=%s", body.name, client_ip(request))
        return {"ok": True, "action": "add_account", "name": body.name,
                "account_count": len(new_cfg.accounts)}

    @app.post("/api/accounts/delete", dependencies=[Depends(require_login)])
    def delete_account(body: DeleteAccountBody, request: Request):
        try:
            new_cfg = store.delete_account(body.name)
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        scheduler.apply_config(new_cfg)
        logger.info("面板删除账号 account=%s ip=%s", body.name, client_ip(request))
        return {"ok": True, "action": "delete_account", "name": body.name,
                "account_count": len(new_cfg.accounts)}

    # ---------------- 热改配置（Phase 4-③，写前自动备份） ----------------

    def _apply(new_cfg: AppConfig, action: str, request: Request):
        # 写盘成功后热应用到调度器；凭证/账号增删等重启项不在这些路由暴露
        scheduler.apply_config(new_cfg)
        logger.info("面板热改 %s ip=%s", action, client_ip(request))
        return {"ok": True, "action": action}

    @app.put("/api/config/accounts/{name}/enabled",
             dependencies=[Depends(require_login)])
    def hot_set_enabled(name: str, body: AccountEnabledBody, request: Request):
        try:
            new_cfg = store.update_account_enabled(name, body.enabled)
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        return _apply(new_cfg, f"账号[{name}]启用={body.enabled}", request)

    @app.put("/api/config/accounts/{name}/interval",
             dependencies=[Depends(require_login)])
    def hot_set_interval(name: str, body: IntervalBody, request: Request):
        try:
            new_cfg = store.update_account_interval(
                name, body.min_interval, body.max_interval
            )
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        return _apply(new_cfg, f"账号[{name}]间隔", request)

    @app.put("/api/config/apis/enabled", dependencies=[Depends(require_login)])
    def hot_set_api(body: ApiEnabledBody, request: Request):
        try:
            new_cfg = store.update_api_enabled(
                body.api_name, body.enabled, body.account_name
            )
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        return _apply(new_cfg, f"API[{body.api_name}]启用={body.enabled}", request)

    @app.put("/api/config/schedule", dependencies=[Depends(require_login)])
    def hot_set_schedule(body: ScheduleBody, request: Request):
        current = scheduler._cfg.schedule.model_dump()
        merged = {
            "enabled": body.enabled if body.enabled is not None else current["enabled"],
            "workdays": body.workdays if body.workdays is not None else current["workdays"],
            "time_windows": body.time_windows
            if body.time_windows is not None else current["time_windows"],
        }
        try:
            new_cfg = store.update_schedule(merged)
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # Pydantic 校验错误统一 400
            raise HTTPException(400, f"Schedule validation failed: {exc}")
        return _apply(new_cfg, "工作时间窗口", request)

    # ---------- 全局默认 / 弹性 / 调用策略（Phase 4-⑤） ----------

    @app.put("/api/config/global/interval",
             dependencies=[Depends(require_login)])
    def hot_global_interval(body: IntervalBody, request: Request):
        try:
            new_cfg = store.update_global_interval(
                body.min_interval, body.max_interval)
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(400, f"Interval validation failed: {exc}")
        return _apply(new_cfg, "全局默认间隔", request)

    @app.put("/api/config/global/concurrency",
             dependencies=[Depends(require_login)])
    def hot_concurrency(body: ConcurrencyBody, request: Request):
        try:
            new_cfg = store.update_concurrency(body.max_concurrent)
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(400, f"Concurrency validation failed: {exc}")
        return _apply(new_cfg, "最大并发", request)

    @app.put("/api/config/global/timeout",
             dependencies=[Depends(require_login)])
    def hot_timeout(body: TimeoutBody, request: Request):
        try:
            new_cfg = store.update_request_timeout(body.timeout)
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(400, f"Timeout validation failed: {exc}")
        return _apply(new_cfg, "请求超时", request)

    @app.put("/api/config/error-handling",
             dependencies=[Depends(require_login)])
    def hot_error_handling(body: ErrorHandlingBody, request: Request):
        try:
            new_cfg = store.update_error_handling(body.model_dump())
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(400, f"Error handling validation failed: {exc}")
        return _apply(new_cfg, "错误处理策略", request)

    @app.put("/api/config/call-policy",
             dependencies=[Depends(require_login)])
    def hot_call_policy(body: CallPolicyBody, request: Request):
        try:
            new_cfg = store.update_call_policy(body.model_dump())
        except HotConfigError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(400, f"Call policy validation failed: {exc}")
        return _apply(new_cfg, "调用策略", request)

    # 静态资源（PWA 外壳，Phase 4 第 5 步挂载）
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        # 缓存策略：HTML 与 sw.js 必须每次校验（否则发版后用户拿旧壳），
        # 带 ?v= 指纹的 js/css/图标可长缓存。避免 max-age=14400 钉死旧版本。
        @app.middleware("http")
        async def cache_control(request: Request, call_next):
            response = await call_next(request)
            path = request.url.path
            if path.endswith(".html") or path in ("/", "/sw.js"):
                response.headers["Cache-Control"] = "no-cache"
            elif path.endswith((".js", ".css", ".svg", ".webmanifest", ".png")):
                response.headers["Cache-Control"] = "public, max-age=86400"
            return response

        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
