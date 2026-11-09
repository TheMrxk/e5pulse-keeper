"""Phase 3 验证：ROPC 密码模式、错误分类打磨、CC/ROPC API 过滤差异。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from src.auth import AuthConfigError, AuthProvider, AuthTransientError
from src.models import (
    AccountConfig,
    AppConfig,
    AuthType,
    ErrorHandlingConfig,
    ScheduleConfig,
)
from src.scheduler import Scheduler

TENANT = "11111111-2222-3333-4444-555555555555"
ROPC_TOKEN = "ropc-token-abcdef123456"
USERS_API = "https://graph.microsoft.com/v1.0/users?$top=5"
ME_API = "https://graph.microsoft.com/v1.0/me"


# ---------------- ROPC 假 MSAL 应用 ----------------

class _FakePublicApp:
    def __init__(self, token_result=None, raise_on_password=False):
        self._token_result = token_result or {
            "access_token": ROPC_TOKEN, "expires_in": 3600
        }
        self._raise = raise_on_password
        self.password_calls = 0
        self.silent_calls = 0

    def get_accounts(self):
        # 首次无缓存账号，模拟 MSAL 真实行为
        return []

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls += 1
        return None

    def acquire_token_by_username_password(self, username, password, scopes):
        self.password_calls += 1
        if self._raise:
            return {"error": "invalid_grant",
                    "error_codes": [50126],
                    "error_description": "The password is wrong"}
        return self._token_result


class _FakePublicAppCached(_FakePublicApp):
    def get_accounts(self):
        return [{"username": "u@x.com"}]

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls += 1
        return {"access_token": ROPC_TOKEN, "expires_in": 3600}


def make_ropc_account(**kw) -> AccountConfig:
    base = {
        "name": "ropc-acct", "enabled": True,
        "auth_type": AuthType.ROPC,
        "tenant_id": TENANT,
        "client_id": "client-ropc",
        "client_secret": "placeholder-secret-1234567890",
        "username": "user@tenant.onmicrosoft.com",
        "password": "user-password-1234567890",
    }
    base.update(kw)
    return AccountConfig(**base)


def make_config(tmp_path: Path, account: AccountConfig) -> AppConfig:
    return AppConfig.model_validate({
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
        },
        "schedule": ScheduleConfig(enabled=False).model_dump(),
        "error_handling": ErrorHandlingConfig().model_dump(),
        "default_apis": [
            {"name": "get_me", "url": ME_API, "enabled": True},
            {"name": "get_users", "url": USERS_API, "enabled": True},
        ],
        "accounts": [account.model_dump(mode="json")],
    })


# ---------------- ROPC 认证 ----------------

@pytest.mark.asyncio
async def test_ropc_get_token_via_password(monkeypatch: pytest.MonkeyPatch) -> None:
    account = make_ropc_account()
    fake = _FakePublicApp()
    monkeypatch.setattr(AuthProvider, "_build_app", lambda self: fake)

    provider = AuthProvider(account)
    token = await provider.get_token()

    assert token == ROPC_TOKEN
    assert fake.password_calls == 1   # 走了用户名密码流
    assert fake.silent_calls == 0     # 无缓存账号，没调 silent


@pytest.mark.asyncio
async def test_ropc_uses_silent_cache_when_account_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_ropc_account()
    fake = _FakePublicAppCached()
    monkeypatch.setattr(AuthProvider, "_build_app", lambda self: fake)

    provider = AuthProvider(account)
    token = await provider.get_token()

    assert token == ROPC_TOKEN
    assert fake.silent_calls == 1
    assert fake.password_calls == 0   # 命中缓存，没重复走密码流


@pytest.mark.asyncio
async def test_ropc_wrong_password_is_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_ropc_account()
    fake = _FakePublicApp(raise_on_password=True)
    monkeypatch.setattr(AuthProvider, "_build_app", lambda self: fake)

    provider = AuthProvider(account)
    with pytest.raises(AuthConfigError, match="configuration"):
        await provider.get_token()


# ---------------- 错误分类打磨 ----------------

def test_classify_mfa_interaction_required_as_fatal() -> None:
    provider = AuthProvider(make_ropc_account())
    err = provider._classify_error({
        "error": "interaction_required",
        "error_codes": [50076],
        "error_description": "MFA required",
    })
    assert isinstance(err, AuthConfigError)


def test_classify_numeric_code_fatal() -> None:
    provider = AuthProvider(make_ropc_account())
    err = provider._classify_error({
        "error": "some_unmapped_id",
        "error_codes": [65001],
        "error_description": "consent missing",
    })
    assert isinstance(err, AuthConfigError)


def test_classify_unknown_error_is_transient() -> None:
    provider = AuthProvider(make_ropc_account())
    err = provider._classify_error({
        "error": "temporarily_unavailable",
        "error_codes": [99999],
        "error_description": "service busy",
    })
    assert isinstance(err, AuthTransientError)


def test_classify_empty_result_is_transient() -> None:
    provider = AuthProvider(make_ropc_account())
    err = provider._classify_error({})
    assert isinstance(err, AuthTransientError)


# ---------------- §5.4 ROPC 不过滤 /me ----------------

def test_ropc_keeps_me_apis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path, make_ropc_account())
    monkeypatch.setattr(AuthProvider, "_build_app",
                        lambda self: _FakePublicApp())
    sched = Scheduler(cfg, asyncio.Event(),
                      transport=httpx.MockTransport(
                          lambda r: httpx.Response(200, json={})))
    apis = sched.effective_apis("ropc-acct")
    names = [a.name for a in apis]
    # ROPC 有用户上下文，/me 必须保留
    assert "get_me" in names
    assert "get_users" in names


def test_cc_still_filters_me(tmp_path: Path) -> None:
    cc_account = AccountConfig(
        name="cc-acct", auth_type=AuthType.CLIENT_CREDENTIALS,
        tenant_id=TENANT, client_id="c1",
        client_secret="cc-secret-1234567890",
    )
    cfg = make_config(tmp_path, cc_account)
    sched = Scheduler(cfg, asyncio.Event())
    names = [a.name for a in sched.effective_apis("cc-acct")]
    assert "get_me" not in names
    assert "get_users" in names


# ---------------- ROPC 端到端一轮 ----------------

@pytest.mark.asyncio
async def test_ropc_full_round_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path, make_ropc_account())
    monkeypatch.setattr(AuthProvider, "_build_app",
                        lambda self: _FakePublicApp())

    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["Authorization"])
        return httpx.Response(200, json={"value": []})

    sched = Scheduler(cfg, asyncio.Event(),
                      transport=httpx.MockTransport(handler))
    results = await sched.run_once()

    assert results["ropc-acct"].success is True
    assert seen_auth and all(a == f"Bearer {ROPC_TOKEN}" for a in seen_auth)
    # ROPC 下 /me 与 /users 都可能被随机抽到，至少发生一次调用
    assert len(seen_auth) >= 1
