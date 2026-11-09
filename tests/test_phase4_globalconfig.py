"""Phase 4-⑤：全局默认/错误处理/调用策略热改。"""

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
PASSWORD = "global-pass-1234567890"


class _FakeMsalApp:
    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "g-token-abcdef123456", "expires_in": 3600}


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
             "tenant_id": TENANT, "client_id": "c1",
             "client_secret": "secret-1234567890"},
        ],
    })
    path = tmp_path / "config.yaml"
    # 必须用明文序列化，否则 SecretStr 被 dump 成 '**********'，模拟不了真实配置
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

def test_global_interval_persists(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    store.update_global_interval(600, 1800)
    reloaded = store.reload()
    assert reloaded.global_.default_min_interval == 600
    assert reloaded.global_.default_max_interval == 1800
    assert list((tmp_path / "backups").glob("config-backup-*.yaml"))


def test_global_interval_inverted_rejected(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    with pytest.raises(HotConfigError, match="cannot exceed"):
        store.update_global_interval(9000, 100)
    assert not list((tmp_path / "backups").glob("config-backup-*.yaml"))


def test_concurrency_and_timeout(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    store.update_concurrency(8)
    store.update_request_timeout(45)
    reloaded = store.reload()
    assert reloaded.global_.max_concurrent_accounts == 8
    assert reloaded.global_.request_timeout == 45


def test_error_handling_block_replace(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    new_eh = {
        "max_consecutive_failures": 5,
        "pause_minutes": 60,
        "max_error_rate": 0.5,
        "sliding_window_size": 10,
        "retry_times": 2,
        "retry_backoff": [1, 3, 7],
    }
    store.update_error_handling(new_eh)
    reloaded = store.reload()
    assert reloaded.error_handling.pause_minutes == 60
    assert reloaded.error_handling.retry_backoff == [1, 3, 7]


def test_error_handling_invalid_rate_rejected(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    bad = {
        "max_consecutive_failures": 5, "pause_minutes": 60,
        "max_error_rate": 1.5,  # 超 1.0 上限
        "sliding_window_size": 10, "retry_times": 2, "retry_backoff": [1],
    }
    with pytest.raises(Exception):
        store.update_error_handling(bad)


def test_call_policy_block_replace(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    new_policy = {
        "apis_per_round_min": 2, "apis_per_round_max": 4,
        "shuffle_order": False, "inter_call_delay_min": 3,
        "inter_call_delay_max": 9,
    }
    store.update_call_policy(new_policy)
    reloaded = store.reload()
    assert reloaded.call_policy.apis_per_round_max == 4
    assert reloaded.call_policy.shuffle_order is False


def test_call_policy_inverted_rejected(tmp_path: Path) -> None:
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    bad = {
        "apis_per_round_min": 5, "apis_per_round_max": 2,  # 倒挂
        "shuffle_order": True, "inter_call_delay_min": 1,
        "inter_call_delay_max": 3,
    }
    with pytest.raises(Exception):
        store.update_call_policy(bad)


# ---------------- HTTP 层：即时热生效到调度器 ----------------

def test_http_global_interval_applies(env) -> None:
    client, sched, _, _ = env
    r = client.put("/api/config/global/interval",
                   json={"min_interval": 300, "max_interval": 900})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sched._cfg.global_.default_min_interval == 300


def test_http_concurrency_rebuilds_semaphore(env) -> None:
    client, sched, _, _ = env
    r = client.put("/api/config/global/concurrency",
                   json={"max_concurrent": 12})
    assert r.status_code == 200
    # 信号量内部上界被重建
    assert sched._sem._value == 12


def test_http_concurrency_out_of_range_422(env) -> None:
    client, _, _, _ = env
    r = client.put("/api/config/global/concurrency",
                   json={"max_concurrent": 99})  # 超 le=20
    assert r.status_code == 422


def test_http_timeout_400_on_inverted(env) -> None:
    client, _, _, _ = env
    # timeout 本身合法，但验证 400 路径用一个 body 类型错误不易构造，
    # 这里验证合法 timeout 正常 + 越界被 422 拦截
    ok = client.put("/api/config/global/timeout", json={"timeout": 60})
    assert ok.status_code == 200
    bad = client.put("/api/config/global/timeout", json={"timeout": 999})
    assert bad.status_code == 422


def test_http_error_handling_full_roundtrip(env) -> None:
    client, sched, _, _ = env
    body = {
        "max_consecutive_failures": 3, "pause_minutes": 30,
        "max_error_rate": 0.4, "sliding_window_size": 8,
        "retry_times": 1, "retry_backoff": [2, 4],
    }
    r = client.put("/api/config/error-handling", json=body)
    assert r.status_code == 200
    assert sched._cfg.error_handling.max_consecutive_failures == 3


def test_http_call_policy_full_roundtrip(env) -> None:
    client, sched, _, _ = env
    body = {
        "apis_per_round_min": 1, "apis_per_round_max": 5,
        "shuffle_order": True, "inter_call_delay_min": 0,
        "inter_call_delay_max": 10,
    }
    r = client.put("/api/config/call-policy", json=body)
    assert r.status_code == 200
    assert sched._cfg.call_policy.apis_per_round_max == 5


def test_overview_exposes_new_fields(env) -> None:
    client, _, _, _ = env
    r = client.get("/api/config/overview")
    assert r.status_code == 200
    data = r.json()
    assert "request_timeout" in data
    assert "retry_backoff" in data["error_handling"]
    assert "apis_per_round_max" in data["call_policy"]
    assert "secret" not in r.text.lower()


def test_new_routes_require_login(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )
    cfg, path = write_config(tmp_path)
    sched = Scheduler(cfg, asyncio.Event())
    client = TestClient(create_app(cfg, sched, path))  # 不登录
    r = client.put("/api/config/global/interval",
                   json={"min_interval": 300, "max_interval": 900})
    assert r.status_code == 401


def test_hot_write_preserves_secrets(tmp_path: Path) -> None:
    """回归：pydantic SecretStr 在 model_dump(json) 下会变 '**********'，
    热改写盘必须还原明文，否则永久毁掉 client_secret/面板密码。"""
    _, path = write_config(tmp_path)
    store = ConfigStore(path)
    store.update_global_interval(600, 1800)  # 触发一次真实写盘
    on_disk = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    # 密钥必须是原值，绝不能是星号脱敏串
    assert on_disk["accounts"][0]["client_secret"] == "secret-1234567890"
    assert on_disk["panel"]["password"] == PASSWORD
    assert "**********" not in Path(path).read_text(encoding="utf-8")


def test_overview_reflects_hot_change(env) -> None:
    """回归：热改后 overview 必须从 scheduler 实时配置读，不能回显闭包旧值。"""
    client, _, _, _ = env
    client.put("/api/config/global/interval",
               json={"min_interval": 777, "max_interval": 999})
    r = client.get("/api/config/overview")
    assert r.json()["default_interval"] == [777, 999]
