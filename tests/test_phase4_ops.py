"""Phase 4-②：手动触发、暂停/恢复操作接口与并发保护。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from src.models import AppConfig
from src.scheduler import Scheduler
from src.web.app import create_app


def dump_config(cfg: AppConfig, tmp_path: Path) -> str:
    p = tmp_path / "config.yaml"
    from src.config_store import dump_config_with_secrets
    p.write_text(yaml.safe_dump(
        dump_config_with_secrets(cfg)
    ), encoding="utf-8")
    return str(p)

TENANT = "11111111-2222-3333-4444-555555555555"
PASSWORD = "op-pass-1234567890"


class _FakeMsalApp:
    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "op-token-abcdef123456", "expires_in": 3600}


def build_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
        },
        "schedule": {"enabled": False},
        "default_apis": [{
            "name": "get_users",
            "url": "https://graph.microsoft.com/v1.0/users?$top=5",
            "enabled": True,
        }],
        "panel": {"enabled": True, "host": "127.0.0.1", "port": 8090,
                  "password": PASSWORD, "login_fail_limit": 5},
        "accounts": [
            {"name": "acct-a", "enabled": True, "auth_type": "client_credentials",
             "tenant_id": TENANT, "client_id": "c1",
             "client_secret": "secret-1234567890"},
            {"name": "acct-b", "enabled": True, "auth_type": "client_credentials",
             "tenant_id": TENANT, "client_id": "c2",
             "client_secret": "secret-1234567890"},
        ],
    })


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg = build_config(tmp_path)
    config_path = dump_config(cfg, tmp_path)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"value": []}))
    sched = Scheduler(cfg, asyncio.Event(), transport=transport)
    app = create_app(cfg, sched, config_path)
    client = TestClient(app)
    client.post("/api/auth/login", json={"password": PASSWORD})
    return client, sched, tmp_path


# ---------------- 鉴权：操作接口未登录被拦 ----------------

def test_ops_require_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg = build_config(tmp_path)
    config_path = dump_config(cfg, tmp_path)
    sched = Scheduler(cfg, asyncio.Event())
    client = TestClient(create_app(cfg, sched, config_path))  # 不登录
    assert client.post("/api/accounts/acct-a/trigger").status_code == 401
    assert client.post("/api/accounts/acct-a/pause").status_code == 401
    assert client.post("/api/accounts/acct-a/resume").status_code == 401


# ---------------- 暂停 / 恢复 ----------------

def test_pause_and_resume(env) -> None:
    client, sched, tmp_path = env
    r = client.post("/api/accounts/acct-a/pause")
    assert r.status_code == 200 and r.json()["manual_pause"] is True
    assert sched.states["acct-a"].manual_pause is True

    r2 = client.post("/api/accounts/acct-a/resume")
    assert r2.status_code == 200 and r2.json()["manual_pause"] is False
    assert sched.states["acct-a"].manual_pause is False


def test_pause_blocks_trigger(env) -> None:
    client, sched, tmp_path = env
    client.post("/api/accounts/acct-a/pause")
    r = client.post("/api/accounts/acct-a/trigger")
    body = r.json()
    assert body["ok"] is False
    assert "paused" in body["reason"].lower()
    # 暂停期间没有真实调用发生
    assert sched.states["acct-a"].total_success == 0


def test_resume_clears_cooldown(env) -> None:
    client, sched, tmp_path = env
    state = sched.states["acct-a"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(sched._cfg.global_.timezone))
    state.trigger_pause(now, sched._cfg.error_handling, "test cooldown")
    state.consecutive_failures = 5
    client.post("/api/accounts/acct-a/resume")
    assert state.pause_until is None
    assert state.consecutive_failures == 0


# ---------------- 手动触发 ----------------

def test_trigger_runs_immediately(env) -> None:
    client, sched, tmp_path = env
    r = client.post("/api/accounts/acct-a/trigger")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["calls"] and body["calls"][0]["success"] is True
    assert sched.states["acct-a"].total_success == 1


def test_trigger_ignores_schedule_and_due(env) -> None:
    # schedule.enabled=false 已绕过窗口；这里验证未到期也能手动触发两次
    client, sched, tmp_path = env
    client.post("/api/accounts/acct-b/trigger")
    r2 = client.post("/api/accounts/acct-b/trigger")
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert sched.states["acct-b"].total_success == 2


def test_trigger_unknown_account_404(env) -> None:
    client, _, _ = env
    r = client.post("/api/accounts/不存在的账号/trigger")
    assert r.status_code == 404


def test_pause_unknown_account_404(env) -> None:
    client, _, _ = env
    assert client.post("/api/accounts/幽灵/pause").status_code == 404


# ---------------- 并发保护：执行中重复触发返回 409 ----------------

@pytest.mark.asyncio
async def test_concurrent_trigger_lock(env) -> None:
    _, sched, _ = env
    results = []

    async def slow_round():
        # 模拟长耗时调用，占用账号锁
        await asyncio.sleep(0.3)
        from src.api_executor import RoundResult
        return RoundResult(success=True)

    # 直接占住账号 A 的锁，验证 trigger_now 的锁冲突判定
    async with sched._run_locks["acct-a"]:
        result = await sched.trigger_now("acct-a")
    assert result["ok"] is False
    assert "already running" in result["reason"].lower()


# ---------------- 多账号隔离：操作 A 不影响 B ----------------

def test_account_ops_isolation(env) -> None:
    client, sched, tmp_path = env
    client.post("/api/accounts/acct-a/pause")
    assert sched.states["acct-a"].manual_pause is True
    assert sched.states["acct-b"].manual_pause is False
    r = client.post("/api/accounts/acct-b/trigger")
    assert r.json()["ok"] is True
    assert sched.states["acct-b"].total_success == 1
