"""Phase 4-⑦：管理员授权向导（设备码流程）测试。

MSAL 设备码与 Graph 调用全部打桩，不触网、不需要真实管理员。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import httpx
import pytest

from src.consent import (
    ConsentManager,
    GRANTABLE_READONLY_ROLES,
    GRAPH_RESOURCE_APP_ID,
)

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TARGET_SP_ID = "sp-target-123"
GRAPH_SP_ID = "sp-graph-456"


class _FakeFlowApp:
    """假公共客户端：initiate_device_flow / acquire_token_by_device_flow。"""

    def __init__(self, token_result=None, raise_exc=None):
        self._token_result = token_result or {
            "access_token": "admin-token-abcdef123456",
            "id_token_claims": {"preferred_username": "admin@tenant.onmicrosoft.com"},
        }
        self._raise = raise_exc

    def initiate_device_flow(self, scopes=None):
        return {
            "user_code": "ABCD-1234",
            "device_code": "device-code-xyz",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "go login",
        }

    def acquire_token_by_device_flow(self, flow):
        if self._raise:
            raise self._raise
        return self._token_result


def _graph_handler(request: httpx.Request) -> httpx.Response:
    """假 Graph：服务主体查询 + appRoleAssignment。"""
    url = str(request.url)
    # appRoleAssignment 路由优先匹配（URL 里也含 servicePrincipals，要先判）
    if "appRoleAssignments" in url and request.method == "GET":
        return httpx.Response(200, json={"value": []})
    if "appRoleAssignments" in url and request.method == "POST":
        return httpx.Response(201, json={"id": "assigned"})
    if "servicePrincipals" in url and request.method == "GET":
        if GRAPH_RESOURCE_APP_ID in url or "appId eq '00000003" in url:
            return httpx.Response(200, json={"value": [{
                "id": GRAPH_SP_ID,
                "appRoles": [
                    {"id": "role-user", "value": "User.Read.All",
                     "allowedMemberTypes": ["Application"]},
                    {"id": "role-group", "value": "Group.Read.All",
                     "allowedMemberTypes": ["Application"]},
                    {"id": "role-write", "value": "Application.ReadWrite.All",
                     "allowedMemberTypes": ["Application"]},
                ],
            }]})
        # 目标应用服务主体
        return httpx.Response(200, json={"value": [{"id": TARGET_SP_ID}]})
    return httpx.Response(404, json={"error": "unexpected"})


def test_grantable_roles_all_readonly():
    # 白名单里绝不允许出现写权限
    assert all(r.endswith(".All") and "Write" not in r for r in GRANTABLE_READONLY_ROLES)
    assert "User.Read.All" in GRANTABLE_READONLY_ROLES


def test_start_session_returns_device_code():
    mgr = ConsentManager()
    with patch("src.consent.msal.PublicClientApplication",
               return_value=_FakeFlowApp()):
        s = mgr.start_session(TENANT, CLIENT)
    assert s.user_code == "ABCD-1234"
    assert s.status == "pending"
    assert "devicelogin" in s.verification_uri
    assert mgr.get_session(s.session_id) is s


def test_poll_grants_only_whitelisted_roles():
    mgr = ConsentManager()
    with patch("src.consent.msal.PublicClientApplication",
               return_value=_FakeFlowApp()):
        s = mgr.start_session(TENANT, CLIENT)

    transport = httpx.MockTransport(_graph_handler)
    # 故意混入一个写权限，验证它被白名单过滤掉
    requested = ["User.Read.All", "Group.Read.All", "Application.ReadWrite.All"]
    with patch("src.consent.httpx.AsyncClient",
               return_value=httpx.AsyncClient(transport=transport)):
        asyncio.run(mgr.poll_and_grant(s.session_id, CLIENT, requested))

    assert s.status == "done"
    assert "Application.ReadWrite.All" not in s.granted_roles
    assert set(s.granted_roles) == {"User.Read.All", "Group.Read.All"}
    assert s.admin_upn == "admin@tenant.onmicrosoft.com"
    # 令牌不保留
    assert s._app is None and s._flow is None


def test_admin_login_failure_marks_session_error():
    fail_app = _FakeFlowApp(token_result={
        "error": "authorization_pending",
        "error_description": "admin declined consent",
    })
    mgr = ConsentManager()
    with patch("src.consent.msal.PublicClientApplication", return_value=fail_app):
        s = mgr.start_session(TENANT, CLIENT)
        asyncio.run(mgr.poll_and_grant(s.session_id, CLIENT, ["User.Read.All"]))
    assert s.status == "error"
    assert "declined" in s.message


def test_exception_during_flow_is_caught():
    mgr = ConsentManager()
    with patch("src.consent.msal.PublicClientApplication",
               return_value=_FakeFlowApp(raise_exc=RuntimeError("network down"))):
        s = mgr.start_session(TENANT, CLIENT)
        asyncio.run(mgr.poll_and_grant(s.session_id, CLIENT, ["User.Read.All"]))
    assert s.status == "error"
    assert "network down" in s.message


def test_get_missing_session_returns_none():
    assert ConsentManager().get_session("nope") is None


# ---------------- HTTP 路由层（TestClient，打桩 ConsentManager） ----------------

def _make_client(monkeypatch):
    from src.web.app import create_app
    from src.models import AppConfig
    from src.scheduler import Scheduler
    import yaml
    from src.config_store import dump_config_with_secrets
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    cfg = AppConfig.model_validate({
        "global": {"log_file": str(tmp / "e.log"),
                   "state_file": str(tmp / "s.json")},
        "schedule": {"enabled": False, "workdays": [1, 2, 3, 4, 5],
                     "time_windows": [{"start": "09:00", "end": "12:00"}]},
        "default_apis": [{"name": "get_users",
                          "url": "https://graph.microsoft.com/v1.0/users",
                          "enabled": True}],
        "panel": {"enabled": True, "password": "consent-pass-1234567890"},
        "accounts": [{"name": "acct-a", "enabled": True,
                      "auth_type": "client_credentials", "tenant_id": TENANT,
                      "client_id": CLIENT, "client_secret": "secret-1234567890"}],
    })
    path = tmp / "config.yaml"
    path.write_text(yaml.safe_dump(dump_config_with_secrets(cfg)))
    sched = Scheduler(cfg, asyncio.Event())
    return create_app(cfg, sched, str(path))


def test_consent_routes_require_login():
    from fastapi.testclient import TestClient
    app = _make_client(None)
    c = TestClient(app)
    assert c.get("/api/consent/roles").status_code == 401
    assert c.post("/api/consent/start",
                  json={"tenant_id": TENANT, "client_id": CLIENT}).status_code == 401


def test_consent_roles_endpoint():
    from fastapi.testclient import TestClient
    app = _make_client(None)
    c = TestClient(app)
    c.post("/api/auth/login", json={"password": "consent-pass-1234567890"})
    r = c.get("/api/consent/roles")
    assert r.status_code == 200
    assert "User.Read.All" in r.json()["roles"]


def test_consent_start_rejects_non_whitelist_role(monkeypatch):
    from fastapi.testclient import TestClient
    app = _make_client(monkeypatch)
    c = TestClient(app)
    c.post("/api/auth/login", json={"password": "consent-pass-1234567890"})

    fake_session = MagicMock(session_id="s1", user_code="CODE",
                             verification_uri="https://microsoft.com/devicelogin",
                             expires_at=__import__("time").time() + 900)
    monkeypatch.setattr(
        "src.web.app.ConsentManager.start_session",
        lambda self, t, cid: fake_session)
    # 全部请求写权限 -> 交集为空 -> 400
    r = c.post("/api/consent/start", json={
        "tenant_id": TENANT, "client_id": CLIENT,
        "roles": ["Application.ReadWrite.All"],
    })
    assert r.status_code == 400


def test_consent_poll_unknown_session_404(monkeypatch):
    from fastapi.testclient import TestClient
    app = _make_client(monkeypatch)
    c = TestClient(app)
    c.post("/api/auth/login", json={"password": "consent-pass-1234567890"})
    monkeypatch.setattr("src.web.app.ConsentManager.get_session",
                        lambda self, sid: None)
    assert c.get("/api/consent/poll/ghost").status_code == 404
