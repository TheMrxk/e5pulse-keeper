from __future__ import annotations

"""
E5Pulse - self-hosted Microsoft 365 E5 keep-alive service.
Copyright (C) 2026 TheMrxk

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

账号状态机（开发文档 §7、§8.3）。

Phase 1 实现完整状态结构，单账号即可验证；Phase 2 由 Scheduler 多账号复用。
两类暂停独立：
- manual_pause：面板手动暂停（运行态，重启复位）
- pause_until：错误触发的冷却暂停（§8.3），到期自动恢复并清零连续失败
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .models import AccountConfig, ErrorHandlingConfig


@dataclass
class AccountState:
    config: AccountConfig
    consecutive_failures: int = 0
    total_success: int = 0
    total_failure: int = 0
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    pause_until: datetime | None = None
    manual_pause: bool = False
    last_error: str | None = None
    # 滑动窗口：True=成功 False=失败
    _window: deque[bool] = field(default_factory=deque)

    def is_paused(self, now: datetime) -> bool:
        if self.manual_pause:
            return True
        if self.pause_until is not None and now >= self.pause_until:
            # 冷却到期，自动恢复（§8.3）
            self.pause_until = None
            self.consecutive_failures = 0
        return self.pause_until is not None

    def record_result(
        self,
        success: bool,
        now: datetime,
        err_cfg: ErrorHandlingConfig,
        error_msg: str | None = None,
    ) -> str | None:
        """记录一轮结果，触发暂停时返回原因字符串，否则 None。"""
        self.last_run_at = now
        self._window.append(success)
        if len(self._window) > err_cfg.sliding_window_size:
            self._window.popleft()

        if success:
            self.total_success += 1
            self.consecutive_failures = 0
            self.last_error = None
            return None

        self.total_failure += 1
        self.consecutive_failures += 1
        self.last_error = error_msg

        if self.consecutive_failures >= err_cfg.max_consecutive_failures:
            return self._pause(
                now, err_cfg,
                f"{self.consecutive_failures} consecutive failures reached threshold",
            )

        if len(self._window) >= err_cfg.sliding_window_size:
            error_rate = self._window.count(False) / len(self._window)
            if error_rate > err_cfg.max_error_rate:
                return self._pause(
                    now, err_cfg,
                    f"sliding-window error rate {error_rate:.0%} exceeded threshold {err_cfg.max_error_rate:.0%}",
                )
        return None

    def _pause(self, now: datetime, err_cfg: ErrorHandlingConfig, reason: str) -> str:
        self.pause_until = now + timedelta(minutes=err_cfg.pause_minutes)
        return reason

    def trigger_pause(self, now: datetime, err_cfg: ErrorHandlingConfig, reason: str) -> str:
        """外部触发暂停（致命错误等），返回暂停原因。"""
        self.pause_until = now + timedelta(minutes=err_cfg.pause_minutes)
        self.last_error = reason
        return reason

    def set_manual_pause(self, paused: bool) -> None:
        self.manual_pause = paused

    def schedule_next(self, next_at: datetime) -> None:
        self.next_run_at = next_at

    def is_due(self, now: datetime) -> bool:
        return self.next_run_at is None or now >= self.next_run_at

    def snapshot(self) -> dict[str, Any]:
        """status.json / 面板用的可序列化快照（§9.3），不含任何 secret。"""
        return {
            "name": self.config.name,
            "enabled": self.config.enabled,
            "auth_type": self.config.auth_type.value,
            "manual_pause": self.manual_pause,
            "pause_until": self.pause_until.isoformat() if self.pause_until else None,
            "consecutive_failures": self.consecutive_failures,
            "total_success": self.total_success,
            "total_failure": self.total_failure,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_error": self.last_error,
        }
