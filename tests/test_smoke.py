"""端到端冒烟：直接驱动 main.amain，MockTransport 拦截全部 Graph 请求。

验证：启动摘要、CC 过滤 WARNING、--once 一轮调用、status.json 落盘、退出码。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from src import main

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write_config(path: Path) -> None:
    cfg = {
        "global": {
            "log_file": str(path / "logs" / "e5pulse.log"),
            "state_file": str(path / "status.json"),
        },
        "schedule": {"enabled": False},
        "default_apis": [
            {"name": "get_me",
             "url": "https://graph.microsoft.com/v1.0/me", "enabled": True},
            {"name": "get_users",
             "url": "https://graph.microsoft.com/v1.0/users?$top=5", "enabled": True},
        ],
        "accounts": [{
            "name": "smoke-acct", "enabled": True,
            "auth_type": "client_credentials",
            "tenant_id": TENANT, "client_id": CLIENT,
            "client_secret": "smoke-secret-1234567890",
        }],
    }
    (path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


class _FakeMsalApp:
    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "smoke-token-abcdef123456", "expires_in": 3600}


def test_amain_once_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path)
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )

    def graph_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"value": []})

    # Scheduler 默认 transport=None（走真实网络），测试注入 MockTransport
    import src.scheduler as scheduler_mod
    original_init = scheduler_mod.Scheduler.__init__

    def patched_init(self, config, stop_event, transport=None):
        original_init(self, config, stop_event,
                      transport=httpx.MockTransport(graph_handler))

    monkeypatch.setattr(scheduler_mod.Scheduler, "__init__", patched_init)

    exit_code = asyncio.run(
        main.amain(str(tmp_path / "config.yaml"), once=True)
    )
    assert exit_code == 0

    status = yaml.safe_load((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["accounts"][0]["total_success"] == 1
    assert status["accounts"][0]["name"] == "smoke-acct"

    log_text = (tmp_path / "logs" / "e5pulse.log").read_text(encoding="utf-8")
    assert "E5Pulse started" in log_text
    assert "auto-filtered 1 /me/* API" in log_text
    # secret 不得落盘
    assert "smoke-secret-1234567890" not in log_text
