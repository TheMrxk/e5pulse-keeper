"""Phase 1 验证测试（开发文档 §14）。

用 httpx MockTransport 拦截全部出站请求，不碰真实网络：
- 配置校验：Pydantic 安检门（坏配置必须被拦）
- §5.4 CC 模式 /me/* 自动过滤
- 全链路：token + 一轮 Graph 调用成功
- 弹性：503 重试成功、429 尊重 Retry-After
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from src.api_executor import GraphExecutor
from src.auth import AuthProvider
from src.config import ConfigError, load_config
from src.models import (
    AccountConfig,
    ApiDefinition,
    AppConfig,
    AuthType,
    CallPolicy,
    ErrorHandlingConfig,
    GlobalConfig,
    HttpMethod,
)
from src.scheduler import Scheduler

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "test-secret-value-1234567890"
MOCK_TOKEN = "mock-token-abcdef123456"


def make_config(tmp_path: Path) -> AppConfig:
    cfg = {
        "global": {
            "log_file": str(tmp_path / "logs" / "e5pulse.log"),
            "state_file": str(tmp_path / "status.json"),
        },
        "schedule": {"enabled": False},
        "default_apis": [
            {"name": "get_me", "method": "GET",
             "url": "https://graph.microsoft.com/v1.0/me", "enabled": True},
            {"name": "get_users", "method": "GET",
             "url": "https://graph.microsoft.com/v1.0/users?$top=5", "enabled": True},
        ],
        "accounts": [
            {
                "name": "test-acct",
                "enabled": True,
                "auth_type": "client_credentials",
                "tenant_id": TENANT,
                "client_id": CLIENT,
                "client_secret": SECRET,
            }
        ],
    }
    return AppConfig.model_validate(cfg)


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def make_transport(graph_handler) -> httpx.MockTransport:
    """统一路由：Graph 请求交给 graph_handler。

    MSAL 的 token 端点由它自己的 requests 会话发出，不经过 httpx，
    测试在 fixture 里 monkeypatch AuthProvider._acquire_cc 直接返回 mock token，
    外部库的内部 HTTP 不属于本项目单测边界。
    """
    return httpx.MockTransport(graph_handler)


@pytest.fixture
def mock_token(monkeypatch: pytest.MonkeyPatch):
    """打桩 MSAL。

    MSAL 构造期会发 OIDC 发现请求、取 token 走自带 requests 会话，
    均不经过 httpx MockTransport；外部库内部 HTTP 不属本项目单测边界，
    这里把整个 MSAL 应用替换成最小假对象。
    """

    class _FakeMsalApp:
        def acquire_token_silent(self, scopes, account=None):
            return None  # 强制走 for_client 分支，验证回退逻辑

        def acquire_token_for_client(self, scopes):
            return {"access_token": MOCK_TOKEN, "expires_in": 3600}

    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    yield MOCK_TOKEN


# ---------------- 配置校验 ----------------

def test_config_validation_bad_interval(tmp_path: Path) -> None:
    bad = {
        "global": {"default_min_interval": 3000, "default_max_interval": 1000,
                   "log_file": str(tmp_path / "x.log"),
                   "state_file": str(tmp_path / "s.json")},
        "schedule": {"enabled": False},
        "default_apis": [{"name": "a", "url": "https://x", "enabled": True}],
        "accounts": [{
            "name": "a", "auth_type": "client_credentials",
            "tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET,
        }],
    }
    p = write_yaml(tmp_path / "bad.yaml", bad)
    with pytest.raises(ConfigError, match="default_min_interval"):
        load_config(p)


def test_config_validation_ropc_missing_password() -> None:
    with pytest.raises(Exception, match="ropc"):
        AccountConfig(
            name="x", auth_type=AuthType.ROPC, tenant_id=TENANT,
            client_id=CLIENT, client_secret=SECRET, username="u@x.com",
        )


def test_config_validation_unknown_field() -> None:
    with pytest.raises(Exception):
        GlobalConfig.model_validate({"no_such_field": 1})


def test_secret_redacted_in_repr() -> None:
    acc = AccountConfig(
        name="x", auth_type=AuthType.CLIENT_CREDENTIALS,
        tenant_id=TENANT, client_id=CLIENT, client_secret=SECRET,
    )
    assert SECRET not in repr(acc)


# ---------------- §5.4 CC 过滤 ----------------

def test_cc_api_filter(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    sched = Scheduler(cfg, asyncio.Event())
    apis = sched.effective_apis("test-acct")
    names = [a.name for a in apis]
    assert "get_me" not in names
    assert "get_users" in names


# ---------------- 调用链路（MockTransport 注入） ----------------

def test_full_round_success(tmp_path: Path, mock_token: str) -> None:
    cfg = make_config(tmp_path)
    def graph_ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {MOCK_TOKEN}"
        return httpx.Response(200, json={"value": []})

    transport = make_transport(graph_ok)

    async def scenario() -> None:
        sched = Scheduler(cfg, asyncio.Event(), transport=transport)
        results = await sched.run_once()
        assert results, "应执行到一个账号"
        assert results["test-acct"].success is True
        state = sched.states["test-acct"]
        assert state.total_success == 1
        assert state.next_run_at is not None
        sched._write_status()
        assert (tmp_path / "status.json").exists()

    asyncio.run(scenario())


def test_retry_on_503_then_success(tmp_path: Path, mock_token: str) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, json={"ok": True})

    cfg = make_config(tmp_path)
    cfg.error_handling.retry_backoff = [0]

    async def scenario() -> None:
        stop = asyncio.Event()
        auth = AuthProvider(cfg.accounts[0])
        executor = GraphExecutor(
            auth, "t",
            [ApiDefinition(name="x", method=HttpMethod.GET,
                           url="https://graph.microsoft.com/v1.0/users")],
            CallPolicy(), cfg.error_handling, 10, stop,
            transport=make_transport(flaky),
        )
        result = await executor.run_round()
        await executor.aclose()
        assert result.success is True
        assert calls["n"] == 2

    asyncio.run(scenario())


def test_429_respects_retry_after(tmp_path: Path, mock_token: str) -> None:
    calls = {"n": 0}

    def rate_limited(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow")
        return httpx.Response(200, json={"ok": True})

    cfg = make_config(tmp_path)

    async def scenario() -> None:
        stop = asyncio.Event()
        auth = AuthProvider(cfg.accounts[0])
        # backoff 给 5s：若没尊重 Retry-After=0，wait_for(3s) 必超时
        executor = GraphExecutor(
            auth, "t",
            [ApiDefinition(name="x", method=HttpMethod.GET,
                           url="https://graph.microsoft.com/v1.0/users")],
            CallPolicy(), ErrorHandlingConfig(retry_backoff=[5]), 10, stop,
            transport=make_transport(rate_limited),
        )
        result = await executor.run_round()
        await executor.aclose()
        assert result.success is True
        assert calls["n"] == 2

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))


def test_auth_header_actually_sent(tmp_path: Path, mock_token: str) -> None:
    """确认 Bearer 头真实带到了 Graph 请求上。"""
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"value": []})

    cfg = make_config(tmp_path)

    async def scenario() -> None:
        stop = asyncio.Event()
        auth = AuthProvider(cfg.accounts[0])
        executor = GraphExecutor(
            auth, "t",
            [ApiDefinition(name="x", method=HttpMethod.GET,
                           url="https://graph.microsoft.com/v1.0/users")],
            CallPolicy(), ErrorHandlingConfig(), 10, stop,
            transport=make_transport(capture),
        )
        result = await executor.run_round()
        await executor.aclose()
        assert result.success
        assert seen["auth"] == f"Bearer {MOCK_TOKEN}"

    asyncio.run(scenario())
