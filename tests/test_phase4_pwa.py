"""Phase 4-④：PWA 前端外壳与静态资源。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.models import AppConfig
from src.scheduler import Scheduler
from src.web.app import create_app

TENANT = "11111111-2222-3333-4444-555555555555"
PASSWORD = "pwa-pass-1234567890"

STATIC_FILES = ["index.html", "app.js", "styles.css", "sw.js",
                "manifest.webmanifest", "icon.svg"]


def build_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
        },
        "schedule": {"enabled": False, "workdays": [1, 2, 3, 4, 5],
                     "time_windows": [{"start": "09:00", "end": "18:00"}]},
        "default_apis": [
            {"name": "get_users",
             "url": "https://graph.microsoft.com/v1.0/users?$top=5",
             "enabled": True},
        ],
        "panel": {"enabled": True, "password": PASSWORD},
        "accounts": [
            {"name": "acct-a", "enabled": True, "auth_type": "client_credentials",
             "tenant_id": TENANT, "client_id": "c1",
             "client_secret": "secret-1234567890"},
        ],
    })


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app",
        lambda self: type("F", (), {
            "acquire_token_silent": lambda *a, **k: None,
            "acquire_token_for_client":
                lambda *a, **k: {"access_token": "t-1234567890", "expires_in": 3600},
        })(),
    )
    cfg = build_config(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text("accounts: []", encoding="utf-8")
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    return TestClient(create_app(cfg, sched, str(path)))


@pytest.mark.parametrize("fname", STATIC_FILES)
def test_static_assets_served(client: TestClient, fname: str) -> None:
    r = client.get(f"/{fname}")
    assert r.status_code == 200
    assert len(r.content) > 0


def test_root_serves_index(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "E5Pulse" in r.text
    assert 'id="login-view"' in r.text


def test_manifest_valid(client: TestClient) -> None:
    r = client.get("/manifest.webmanifest")
    manifest = json.loads(r.text)
    assert manifest["display"] == "standalone"
    assert manifest["name"].startswith("E5Pulse")
    assert manifest["icons"], "必须声明图标"


def test_service_worker_never_caches_api() -> None:
    sw = (Path(__file__).resolve().parents[1]
          / "src/web/static/sw.js").read_text(encoding="utf-8")
    assert "/api/" in sw and "return" in sw  # API 直接放行不缓存
    assert "e5pulse-shell" in sw


def test_overview_includes_default_apis(client: TestClient) -> None:
    client.post("/api/auth/login", json={"password": PASSWORD})
    r = client.get("/api/config/overview")
    assert r.status_code == 200
    apis = r.json()["default_apis"]
    assert {"name", "method", "url", "enabled"} <= set(apis[0].keys())
    # 脱敏：概览绝不含 secret
    assert "secret" not in r.text.lower()


def test_frontend_html_references_local_assets_only() -> None:
    html = (Path(__file__).resolve().parents[1]
            / "src/web/static/index.html").read_text(encoding="utf-8")
    # 不允许外网 CDN（自托管/离线可用原则）
    assert "http://" not in html and "https://" not in html
    assert "app.js" in html and "styles.css" in html


def test_html_and_sw_no_cache(client: TestClient) -> None:
    """HTML/sw.js 必须 no-cache，带版本指纹的静态资源可长缓存。"""
    html = client.get("/")
    assert html.headers.get("cache-control") == "no-cache"
    sw = client.get("/sw.js")
    assert sw.headers.get("cache-control") == "no-cache"
    js = client.get("/app.js")
    assert "max-age" in (js.headers.get("cache-control") or "")


def test_index_references_versioned_assets() -> None:
    """发版防缓存：index.html 必须引用带 ?v= 指纹的 js/css。"""
    html = (Path(__file__).resolve().parents[1]
            / "src/web/static/index.html").read_text(encoding="utf-8")
    assert "app.js?v=" in html and "styles.css?v=" in html
