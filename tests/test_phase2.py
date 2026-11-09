"""Phase 2 验证：多账号调度、工作时间窗口、智能暂停、长期运行。

所有时间用固定时钟注入，不依赖真实 now()，保证测试稳定可重复。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.account import AccountState
from src.models import (
    AccountConfig,
    ApiDefinition,
    AppConfig,
    AuthType,
    ErrorHandlingConfig,
    HttpMethod,
    ScheduleConfig,
    TimeWindow,
)
from src.scheduler import Scheduler
from src.utils import in_schedule

TENANT = "11111111-2222-3333-4444-555555555555"
TZ = "Asia/Shanghai"
USERS_API = "https://graph.microsoft.com/v1.0/users?$top=5"
MOCK_TOKEN = "phase2-token-abcdef123456"


class _FakeMsalApp:
    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": MOCK_TOKEN, "expires_in": 3600}


@pytest.fixture
def mock_msal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.auth.AuthProvider._build_app", lambda self: _FakeMsalApp()
    )


def make_account(name: str, **kw) -> AccountConfig:
    base = {
        "name": name, "enabled": True,
        "auth_type": AuthType.CLIENT_CREDENTIALS,
        "tenant_id": TENANT,
        "client_id": f"client-{name}",
        "client_secret": f"secret-{name}-1234567890",
        "min_interval": 60, "max_interval": 120,
    }
    base.update(kw)
    return AccountConfig(**base)


def make_config(tmp_path: Path, accounts: list[AccountConfig],
                schedule: ScheduleConfig | None = None,
                err: ErrorHandlingConfig | None = None) -> AppConfig:
    return AppConfig.model_validate({
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
            "timezone": TZ,
        },
        "schedule": schedule or ScheduleConfig(enabled=False),
        "error_handling": err or ErrorHandlingConfig(),
        "default_apis": [{
            "name": "get_users", "method": "GET",
            "url": USERS_API, "enabled": True,
        }],
        "accounts": [a.model_dump(mode="json") for a in accounts],
    })


def transport_with(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------- 工作时间窗口（纯函数，固定时间） ----------------

def test_schedule_workday_and_window() -> None:
    # 2026-11-09 是周一
    monday_10am = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo(TZ))
    monday_13pm = datetime(2026, 11, 9, 13, 0, tzinfo=ZoneInfo(TZ))
    sunday = datetime(2026, 11, 8, 10, 0, tzinfo=ZoneInfo(TZ))

    cfg = ScheduleConfig(
        enabled=True, workdays=[1, 2, 3, 4, 5],
        time_windows=[TimeWindow(start="09:00", end="12:00"),
                      TimeWindow(start="14:00", end="22:00")],
    )
    assert in_schedule(monday_10am, cfg) is True       # 窗口内
    assert in_schedule(monday_13pm, cfg) is False      # 午休窗口外
    assert in_schedule(sunday, cfg) is False           # 非工作日


def test_schedule_disabled_always_open() -> None:
    cfg = ScheduleConfig(enabled=True, workdays=[6, 7],
                         time_windows=[TimeWindow(start="00:00", end="01:00")])
    # enabled=false 时一切放行
    cfg.enabled = False
    now = datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo(TZ))
    assert in_schedule(now, cfg) is True


# ---------------- 智能暂停状态机 ----------------

def test_consecutive_failures_trigger_pause(tmp_path: Path) -> None:
    err_cfg = ErrorHandlingConfig(
        max_consecutive_failures=3, pause_minutes=180,
        sliding_window_size=20, max_error_rate=0.6,
    )
    state = AccountState(config=make_account("a"))
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo(TZ))

    assert state.record_result(False, now, err_cfg, "e1") is None
    assert state.record_result(False, now, err_cfg, "e2") is None
    reason = state.record_result(False, now, err_cfg, "e3")
    assert reason is not None and "3 consecutive failures" in reason
    assert state.is_paused(now) is True
    assert state.pause_until == now.replace(hour=13)  # 10:00 + 180min


def test_pause_auto_recovers_after_cooldown(tmp_path: Path) -> None:
    err_cfg = ErrorHandlingConfig(max_consecutive_failures=1, pause_minutes=60)
    state = AccountState(config=make_account("a"))
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo(TZ))

    state.record_result(False, now, err_cfg, "boom")
    assert state.is_paused(now) is True

    # 冷却期内仍暂停
    assert state.is_paused(datetime(2026, 11, 9, 10, 30, tzinfo=ZoneInfo(TZ))) is True
    # 到期自动恢复并清零连续失败（§8.3）
    recovered = datetime(2026, 11, 9, 11, 1, tzinfo=ZoneInfo(TZ))
    assert state.is_paused(recovered) is False
    assert state.consecutive_failures == 0
    assert state.pause_until is None


def test_sliding_window_error_rate_pause(tmp_path: Path) -> None:
    err_cfg = ErrorHandlingConfig(
        max_consecutive_failures=99,  # 不触发连续失败，专测错误率
        pause_minutes=60, sliding_window_size=5, max_error_rate=0.6,
    )
    state = AccountState(config=make_account("a"))
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo(TZ))

    # 窗口攒满 5 次：3 失败 = 60%，阈值是 >0.6 才触发，故 3/5 不触发
    results = [True, False, True, False, False]
    reason = None
    for ok in results:
        reason = state.record_result(ok, now, err_cfg)
    assert reason is None
    # 再来一次失败，窗口滑动后错误率超 60%
    reason = state.record_result(False, now, err_cfg, "high rate")
    assert reason is not None and "error rate" in reason


def test_manual_pause_independent_from_cooldown() -> None:
    err_cfg = ErrorHandlingConfig()
    state = AccountState(config=make_account("a"))
    now = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo(TZ))

    state.set_manual_pause(True)
    assert state.is_paused(now) is True
    state.set_manual_pause(False)
    assert state.is_paused(now) is False  # 无冷却时立即恢复


# ---------------- 多账号并发调度 ----------------

@pytest.mark.asyncio
async def test_multi_account_run_once(tmp_path: Path, mock_msal) -> None:
    accounts = [make_account("acct-a"), make_account("acct-b"), make_account("acct-c")]
    cfg = make_config(tmp_path, accounts)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json={"value": []})

    sched = Scheduler(cfg, asyncio.Event(), transport=transport_with(handler))
    results = await sched.run_once()

    assert set(results.keys()) == {"acct-a", "acct-b", "acct-c"}
    assert all(r.success for r in results.values())
    assert len(calls) == 3  # 每账号一轮一次调用
    for name in ("acct-a", "acct-b", "acct-c"):
        st = sched.states[name]
        assert st.total_success == 1
        assert st.last_run_at is not None
        assert st.next_run_at is not None and st.next_run_at > st.last_run_at


@pytest.mark.asyncio
async def test_due_guard_skips_account_before_next_run(
    tmp_path: Path, mock_msal
) -> None:
    accounts = [make_account("A")]
    cfg = make_config(tmp_path, accounts)
    calls = {"n": 0}
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=transport_with(
            lambda r: (calls.__setitem__("n", calls["n"] + 1),
                       httpx.Response(200, json={}))[1]
        ),
    )

    await sched.run_once()
    assert calls["n"] == 1
    # next_run_at 已被设到未来，第二轮应跳过
    await sched.run_once()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_paused_account_skipped(tmp_path: Path, mock_msal) -> None:
    accounts = [make_account("A")]
    cfg = make_config(tmp_path, accounts)
    calls = {"n": 0}
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=transport_with(
            lambda r: (calls.__setitem__("n", calls["n"] + 1),
                       httpx.Response(200, json={}))[1]
        ),
    )
    sched.states["A"].set_manual_pause(True)
    results = await sched.run_once()
    assert results == {}
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_disabled_account_never_runs(tmp_path: Path, mock_msal) -> None:
    # 模型禁止"全部账号禁用"启动，故用一启用一禁用，验证禁用者不被调度
    accounts = [make_account("enabled-a"), make_account("disabled-b", enabled=False)]
    cfg = make_config(tmp_path, accounts)
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=transport_with(lambda r: httpx.Response(200, json={})),
    )
    results = await sched.run_once()
    assert set(results.keys()) == {"enabled-a"}
    assert sched.states["disabled-b"].last_run_at is None


# ---------------- 失败 → 暂停 → 恢复 完整流转 ----------------

@pytest.mark.asyncio
async def test_failure_flow_pauses_and_recovers(
    tmp_path: Path, mock_msal
) -> None:
    err_cfg = ErrorHandlingConfig(
        max_consecutive_failures=2, pause_minutes=60, sliding_window_size=20
    )
    accounts = [make_account("A")]
    cfg = make_config(tmp_path, accounts, err=err_cfg)

    # 前两轮失败（重试耗尽后 RoundResult 仍失败）
    fail_then_ok = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fail_then_ok["n"] += 1
        if fail_then_ok["n"] <= 2:
            return httpx.Response(500, text="err")
        return httpx.Response(200, json={"ok": True})

    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=transport_with(handler),
    )
    # retry_times=0 让失败快速返回，不拖时间
    cfg.error_handling.retry_times = 0

    await sched.run_once()  # 第 1 次失败
    state = sched.states["A"]
    assert state.consecutive_failures == 1

    # 强制到期再跑一轮，第 2 次失败 → 触发暂停
    state.next_run_at = None
    await sched.run_once()
    assert state.pause_until is not None
    assert state.last_run_at is not None
    assert state.is_paused(state.last_run_at) is True

    # 暂停期间巡检：不会再发请求
    n_before = fail_then_ok["n"]
    state.next_run_at = None
    await sched.run_once()
    assert fail_then_ok["n"] == n_before

    # 模拟冷却结束：清掉暂停后再跑，成功并清零计数
    state.pause_until = None
    state.consecutive_failures = 0
    state.next_run_at = None
    await sched.run_once()
    assert state.total_success == 1
    assert state.consecutive_failures == 0


# ---------------- 并发隔离：账号间错误互不影响 ----------------

@pytest.mark.asyncio
async def test_account_isolation_one_fails_others_ok(
    tmp_path: Path, mock_msal
) -> None:
    accounts = [make_account("good-a"), make_account("bad-b"), make_account("good-c")]
    cfg = make_config(tmp_path, accounts)
    cfg.error_handling.retry_times = 0

    def handler(request: httpx.Request) -> httpx.Response:
        # 坏账号的 client_id 会进 authority，但这里按调用顺序难区分，
        # 改用计数：第 2 个账号的请求返回 403
        return httpx.Response(200, json={})

    # 通过独立 transport 按 URL 无法区分账号，改用 monkeypatch 让某账号 executor 失败
    from src.api_executor import GraphExecutor
    original_run = GraphExecutor.run_round
    call_order: list[str] = []

    async def patched_run(self):
        call_order.append(self._name)
        if self._name == "bad-b":
            from src.api_executor import RoundResult
            return RoundResult(success=False, fatal=True, error="simulated fatal credential error")
        return await original_run(self)

    import src.api_executor as exe_mod
    exe_mod.GraphExecutor.run_round = patched_run
    try:
        sched = Scheduler(
            cfg, asyncio.Event(), transport=transport_with(handler)
        )
        results = await sched.run_once()
        assert results["good-a"].success is True
        assert results["good-c"].success is True
        assert results["bad-b"].success is False
        # 坏账号被致命错误暂停，好账号不受影响
        assert sched.states["bad-b"].pause_until is not None
        assert sched.states["good-a"].pause_until is None
        assert set(call_order) == {"good-a", "bad-b", "good-c"}
    finally:
        exe_mod.GraphExecutor.run_round = original_run


# ---------------- 长期主循环可被停止事件优雅打断 ----------------

@pytest.mark.asyncio
async def test_run_forever_stops_gracefully(
    tmp_path: Path, mock_msal
) -> None:
    accounts = [make_account("A")]
    cfg = make_config(tmp_path, accounts)
    # 极小巡检间隔快速验证循环与停止，避免 0 值空转烧 CPU
    sched = Scheduler(
        cfg, asyncio.Event(),
        transport=transport_with(lambda r: httpx.Response(200, json={})),
        loop_interval=1,
    )

    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.15)
        stop.set()

    # Scheduler 内部用自己构造的 event，这里把停止事件换成可控的
    sched._stop = stop
    await asyncio.gather(sched.run_forever(), _stop_soon())
    # 能走到这里说明主循环正常退出、没有卡死或抛异常
    assert (tmp_path / "status.json").exists()
