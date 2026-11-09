"""Phase 4-③：配置热改、写前备份、重启边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from src.config_store import ConfigStore, HotConfigError
from src.models import AppConfig
from src.scheduler import Scheduler
from src.web.app import create_app

TENANT = "11111111-2222-3333-4444-555555555555"
PASSWORD = "hot-pass-1234567890"


class _FakeMsalApp:
    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "hot-token-abcdef123456", "expires_in": 3600}


def write_config(tmp_path: Path) -> tuple[AppConfig, str]:
    cfg = AppConfig.model_validate({
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
        },
        "schedule": {"enabled": True, "workdays": [1, 2, 3, 4, 5],
                     "time_windows": [{"start": "09:00", "end": "12:00"}]},
        "default_apis": [
            {"name": "get_users",
             "url": "https://graph.microsoft.com/v1.0/users?$top=5",
             "enabled": True},
            {"name": "get_org",
             "url": "https://graph.microsoft.com/v1.0/organization",
             "enabled": False},
        ],
        "panel": {"enabled": True, "password": PASSWORD},
        "accounts": [
            {"name": "acct-a", "enabled": True, "auth_type": "client_credentials",
             "tenant_id": TENANT, "client_id": "c1",
             "client_secret": "secret-1234567890"},
        ],
    })
    path = tmp_path / "config.yaml"
    from src.config_store import dump_config_with_secrets
    path.write_text(yaml.safe_dump(
        dump_config_with_secrets(cfg)
    ), encoding="utf-8")
    return cfg, str(path)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg, path = write_config(tmp_path)
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    client = TestClient(create_app(cfg, sched, path))
    client.post("/api/auth/login", json={"password": PASSWORD})
    return client, sched, path, tmp_path


# ---------------- ConfigStore 单元层 ----------------

def test_update_enabled_writes_and_backs_up(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    store.update_account_enabled("acct-a", False)

    on_disk = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assert on_disk["accounts"][0]["enabled"] is False
    backups = list((tmp_path / "backups").glob("config-backup-*.yaml"))
    assert len(backups) == 1  # 写前备份


def test_interval_validation_rejects_inverted(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    with pytest.raises(HotConfigError, match="cannot exceed"):
        store.update_account_interval("acct-a", 9999, 100)
    # 被拒绝后磁盘不应有备份/改动
    assert not list((tmp_path / "backups").glob("config-backup-*.yaml"))


def test_update_api_toggle(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    store.update_api_enabled("get_org", True)
    reloaded = store.reload()
    api = next(a for a in reloaded.default_apis if a.name == "get_org")
    assert api.enabled is True


def test_unknown_account_raises(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    with pytest.raises(HotConfigError, match="account not found"):
        ConfigStore(path).update_account_enabled("ghost", True)


# ---------------- HTTP 层：热改即时生效到调度器 ----------------

def test_hot_disable_account_then_not_scheduled(env) -> None:
    client, sched, path, tmp_path = env
    r = client.put("/api/config/accounts/acct-a/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["ok"] is True

    # 调度器内存配置已更新
    account = next(a for a in sched._cfg.accounts if a.name == "acct-a")
    assert account.enabled is False
    # 跑一轮不会执行该账号
    results = asyncio.run(sched.run_once())
    assert "acct-a" not in results


def test_hot_interval_change_applies(env) -> None:
    client, sched, _, _ = env
    r = client.put(
        "/api/config/accounts/acct-a/interval",
        json={"min_interval": 100, "max_interval": 200},
    )
    assert r.status_code == 200
    lo, hi = next(
        a for a in sched._cfg.accounts if a.name == "acct-a"
    ).effective_interval(sched._cfg.global_)
    assert (lo, hi) == (100, 200)


def test_hot_interval_bad_request_400(env) -> None:
    client, _, _, _ = env
    r = client.put(
        "/api/config/accounts/acct-a/interval",
        json={"min_interval": 500, "max_interval": 100},
    )
    assert r.status_code == 400


def test_hot_schedule_change(env) -> None:
    client, sched, _, _ = env
    r = client.put("/api/config/schedule", json={
        "enabled": True, "workdays": [6, 7],
        "time_windows": [{"start": "10:00", "end": "11:00"}],
    })
    assert r.status_code == 200
    assert sched._cfg.schedule.workdays == [6, 7]


def test_hot_schedule_invalid_window_400(env) -> None:
    client, _, _, _ = env
    r = client.put("/api/config/schedule", json={
        "enabled": True, "workdays": [1],
        "time_windows": [{"start": "18:00", "end": "09:00"}],  # 倒挂
    })
    assert r.status_code == 400


def test_hot_requires_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg, path = write_config(tmp_path)
    sched = Scheduler(cfg, asyncio.Event())
    client = TestClient(create_app(cfg, sched, path))  # 不登录
    r = client.put("/api/config/accounts/acct-a/enabled", json={"enabled": False})
    assert r.status_code == 401


def test_apply_config_preserves_runtime_stats(env) -> None:
    client, sched, _, _ = env
    # 先触发一次产生统计
    r = client.post("/api/accounts/acct-a/trigger")
    assert r.json()["ok"] is True
    success_before = sched.states["acct-a"].total_success
    assert success_before >= 1

    # 热改后统计保留
    client.put("/api/config/accounts/acct-a/interval",
               json={"min_interval": 60, "max_interval": 120})
    assert sched.states["acct-a"].total_success == success_before


def test_disable_then_reenable_cycle(env) -> None:
    """全停用后再启用：验证 lenient reload 不断链。"""
    client, sched, _, _ = env
    r1 = client.put("/api/config/accounts/acct-a/enabled",
                    json={"enabled": False})
    assert r1.status_code == 200
    r2 = client.put("/api/config/accounts/acct-a/enabled",
                    json={"enabled": True})
    assert r2.status_code == 200
    account = next(a for a in sched._cfg.accounts if a.name == "acct-a")
    assert account.enabled is True
    # 启用后能正常触发
    r3 = client.post("/api/accounts/acct-a/trigger")
    assert r3.json()["ok"] is True
