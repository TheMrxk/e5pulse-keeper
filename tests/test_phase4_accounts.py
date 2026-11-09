"""Phase 4-⑥：面板在线增删账号（CC 模式，保存前真实校验凭证）。"""

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
TENANT2 = "99999999-aaaa-bbbb-cccc-dddddddddddd"
PASSWORD = "acct-mgmt-pass-1234567890"


class _FakeMsalApp:
    """根据 client_id 决定成功/失败，模拟凭证验证。"""

    def __init__(self, fail: bool = False):
        self._fail = fail

    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        if self._fail:
            return {"error": "invalid_client",
                    "error_codes": [700021],
                    "error_description": "invalid secret\r\ntrace"}
        return {"access_token": "fake-token-abcdef123456", "expires_in": 3600}


def write_config(tmp_path: Path) -> tuple[AppConfig, str]:
    cfg = AppConfig.model_validate({
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
        },
        "schedule": {"enabled": False, "workdays": [1, 2, 3, 4, 5],
                     "time_windows": [{"start": "09:00", "end": "12:00"}]},
        "default_apis": [
            {"name": "get_users",
             "url": "https://graph.microsoft.com/v1.0/users?$top=5",
             "enabled": True},
        ],
        "panel": {"enabled": True, "password": PASSWORD},
        "accounts": [
            {"name": "acct-a", "enabled": True, "auth_type": "client_credentials",
             "tenant_id": TENANT, "client_id": "cid-1",
             "client_secret": "secret-1234567890"},
        ],
    })
    path = tmp_path / "config.yaml"
    from src.config_store import dump_config_with_secrets
    path.write_text(yaml.safe_dump(dump_config_with_secrets(cfg)), encoding="utf-8")
    return cfg, str(path)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 默认凭证成功；用 client_id 含 "bad" 时模拟失败
    def fake_build(self):
        return _FakeMsalApp(fail="bad" in self._account.client_id)
    monkeypatch.setattr("src.auth.AuthProvider._build_app", fake_build)

    cfg, path = write_config(tmp_path)
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    client = TestClient(create_app(cfg, sched, path))
    client.post("/api/auth/login", json={"password": PASSWORD})
    return client, sched, path, tmp_path


# ---------------- ConfigStore 单元层 ----------------

def test_add_account_persists(env) -> None:
    client, sched, path, tmp_path = env
    r = client.post("/api/accounts", json={
        "name": "acct-b", "tenant_id": TENANT2,
        "client_id": "cid-2", "client_secret": "secret2-1234567890",
    })
    assert r.status_code == 200, r.text
    assert r.json()["account_count"] == 2
    on_disk = yaml.safe_load(Path(path).read_text())
    names = [a["name"] for a in on_disk["accounts"]]
    assert "acct-b" in names
    # 密钥以明文正确写回（不是星号）
    new_secret = next(a["client_secret"] for a in on_disk["accounts"]
                      if a["name"] == "acct-b")
    assert new_secret == "secret2-1234567890"
    # 调度器热生效
    assert "acct-b" in sched.states


def test_add_duplicate_name_rejected(env) -> None:
    client, _, _, _ = env
    r = client.post("/api/accounts", json={
        "name": "acct-a", "tenant_id": TENANT2,
        "client_id": "cid-x", "client_secret": "secretx-1234567890",
    })
    assert r.status_code == 400 and "already exists" in r.json()["detail"]


def test_add_duplicate_tenant_client_rejected(env) -> None:
    client, _, _, _ = env
    r = client.post("/api/accounts", json={
        "name": "different-name", "tenant_id": TENANT,
        "client_id": "cid-1", "client_secret": "secret-1234567890",
    })
    assert r.status_code == 400 and "already exists" in r.json()["detail"]


def test_invalid_credential_rejected_and_not_saved(env) -> None:
    client, _, path, _ = env
    r = client.post("/api/accounts", json={
        "name": "acct-bad", "tenant_id": TENANT2,
        "client_id": "bad-cid", "client_secret": "wrong-secret-12345",
    })
    assert r.status_code == 400 and "Credential validation failed" in r.json()["detail"]
    # 验证失败绝不写盘
    on_disk = yaml.safe_load(Path(path).read_text())
    assert all(a["name"] != "acct-bad" for a in on_disk["accounts"])


def test_bogus_tenant_returns_400_not_500(env, monkeypatch) -> None:
    """假 tenant GUID 会让 MSAL 构造期就抛 ValueError，必须转成 400 而非 500。"""
    client, _, path, _ = env

    def boom(self):
        raise ValueError("Unable to get authority configuration")
    monkeypatch.setattr("src.auth.AuthProvider._build_app", boom)
    r = client.post("/api/accounts", json={
        "name": "acct-bogus", "tenant_id": "00000000-0000-0000-0000-000000000000",
        "client_id": "cid-x", "client_secret": "secretx-1234567890",
    })
    assert r.status_code == 400
    on_disk = yaml.safe_load(Path(path).read_text())
    assert all(a["name"] != "acct-bogus" for a in on_disk["accounts"])


def test_delete_account(env) -> None:
    client, sched, path, _ = env
    # 先加一个可删的
    client.post("/api/accounts", json={
        "name": "acct-b", "tenant_id": TENANT2,
        "client_id": "cid-2", "client_secret": "secret2-1234567890",
    })
    r = client.post("/api/accounts/delete", json={"name": "acct-b"})
    assert r.status_code == 200 and r.json()["account_count"] == 1
    assert "acct-b" not in sched.states
    on_disk = yaml.safe_load(Path(path).read_text())
    assert all(a["name"] != "acct-b" for a in on_disk["accounts"])


def test_cannot_delete_last_account(env) -> None:
    client, _, _, _ = env
    r = client.post("/api/accounts/delete", json={"name": "acct-a"})
    assert r.status_code == 400 and "at least one account" in r.json()["detail"]


def test_delete_nonexistent_returns_400(env) -> None:
    client, _, _, _ = env
    r = client.post("/api/accounts/delete", json={"name": "ghost"})
    assert r.status_code == 400


def test_add_requires_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.auth.AuthProvider._build_app",
                        lambda self: _FakeMsalApp())
    cfg, path = write_config(tmp_path)
    sched = Scheduler(cfg, asyncio.Event())
    anon = TestClient(create_app(cfg, sched, path))  # 不登录
    r = anon.post("/api/accounts", json={
        "name": "x", "tenant_id": TENANT2,
        "client_id": "c", "client_secret": "s-1234567890",
    })
    assert r.status_code == 401


def test_backup_created_on_add(env) -> None:
    client, _, _, tmp_path = env
    client.post("/api/accounts", json={
        "name": "acct-b", "tenant_id": TENANT2,
        "client_id": "cid-2", "client_secret": "secret2-1234567890",
    })
    backups = list((tmp_path / "backups").glob("config-backup-*.yaml"))
    assert backups, "新增账号前应自动备份"


def test_store_add_rejects_ropc(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    # 即便伪造 auth_type=ropc，store 也强制改回 CC，保证在线入口不引入 ROPC
    cfg = store.add_account({
        "name": "forced", "tenant_id": TENANT2,
        "client_id": "cid-9", "client_secret": "s-1234567890",
        "auth_type": "ropc",
    })
    added = next(a for a in cfg.accounts if a.name == "forced")
    assert added.auth_type.value == "client_credentials"
