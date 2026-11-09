"""Phase 4 第一步：面板登录鉴权、防爆破、只读接口、健康检查。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import yaml

from src.models import AppConfig
from src.scheduler import Scheduler
from src.web.app import create_app
from src.web.auth_session import COOKIE_NAME


def dump_config(cfg: AppConfig, tmp_path: Path) -> str:
    """把配置模型落盘，返回路径，供 ConfigStore 热改写回。"""
    p = tmp_path / "config.yaml"
    from src.config_store import dump_config_with_secrets
    p.write_text(yaml.safe_dump(
        dump_config_with_secrets(cfg)
    ), encoding="utf-8")
    return str(p)

TENANT = "11111111-2222-3333-4444-555555555555"
PANEL_PASSWORD = "panel-pass-1234567890"


class _FakeMsalApp:
    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "panel-token-abcdef123456", "expires_in": 3600}


def build_config(tmp_path: Path, fail_limit: int = 3) -> AppConfig:
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
        "panel": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8090,
            "password": PANEL_PASSWORD,
            "login_fail_limit": fail_limit,
        },
        "accounts": [{
            "name": "panel-acct", "enabled": True,
            "auth_type": "client_credentials",
            "tenant_id": TENANT, "client_id": "c1",
            "client_secret": "secret-1234567890",
        }],
    })


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg = build_config(tmp_path)
    config_path = dump_config(cfg, tmp_path)
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    app = create_app(cfg, sched, config_path)
    return TestClient(app)


# ---------------- 健康检查无需登录 ----------------

def test_health_open(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------- 未登录被拦 ----------------

def test_protected_requires_login(client: TestClient) -> None:
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/config/overview").status_code == 401
    assert client.get("/api/logs").status_code == 401


# ---------------- 登录成功拿 cookie ----------------

def test_login_success_sets_cookie(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"password": PANEL_PASSWORD})
    assert resp.status_code == 200
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie
    # 登录响应已让 client 持久化 cookie，直接访问受保护接口
    authed = client.get("/api/status")
    assert authed.status_code == 200
    assert authed.json()["accounts"][0]["name"] == "panel-acct"


def test_login_wrong_password(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401
    assert COOKIE_NAME not in client.cookies


# ---------------- 防爆破：超阈值锁定 ----------------

def test_login_lockout_after_failures(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg = build_config(tmp_path, fail_limit=3)
    config_path = dump_config(cfg, tmp_path)
    sched = Scheduler(cfg, asyncio.Event())
    app = create_app(cfg, sched, config_path)
    c = TestClient(app)

    for _ in range(3):
        assert c.post("/api/auth/login", json={"password": "bad"}).status_code == 401
    # 第 4 次即便密码正确也被限流
    locked = c.post("/api/auth/login", json={"password": PANEL_PASSWORD})
    assert locked.status_code == 429
    assert "try again in" in locked.json()["detail"]


# ---------------- 登出失效 ----------------

def test_logout_clears_session(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"password": PANEL_PASSWORD})
    cookie = resp.cookies.get(COOKIE_NAME)
    assert client.get("/api/status", cookies={COOKIE_NAME: cookie}).status_code == 200
    client.post("/api/auth/logout")
    # 登出后 cookie 被删
    assert client.cookies.get(COOKIE_NAME) is None


# ---------------- 伪造 cookie 被拒 ----------------

def test_forged_cookie_rejected(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "forged.signature.here")
    resp = client.get("/api/status")
    assert resp.status_code == 401


# ---------------- 配置概览不泄露 secret ----------------

def test_overview_redacts_secret(client: TestClient) -> None:
    client.post("/api/auth/login", json={"password": PANEL_PASSWORD})
    resp = client.get("/api/config/overview")
    assert resp.status_code == 200
    body = resp.text
    assert "secret-1234567890" not in body       # client_secret 不回传
    data = resp.json()
    assert data["accounts"][0]["auth_type"] == "client_credentials"
    assert "interval" in data["accounts"][0]


# ---------------- 日志接口 ----------------

def test_logs_tail(client: TestClient, tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "e5.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
    client.post("/api/auth/login", json={"password": PANEL_PASSWORD})
    resp = client.get("/api/logs?lines=2")
    assert resp.status_code == 200
    assert resp.json()["lines"] == ["line2", "line3"]  # 只留尾部 2 行
