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

通用工具：时区、时间窗口、随机策略。
"""

import asyncio
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import ApiDefinition, ScheduleConfig, TimeWindow


def now_in_tz(tz_name: str) -> datetime:
    """按配置时区取当前时间（窗口判断必须用本地时区，不能用 UTC 裸比）。"""
    return datetime.now(ZoneInfo(tz_name))


def _within_window(now: datetime, window: TimeWindow) -> bool:
    cur = now.hour * 60 + now.minute
    sh, sm = (int(x) for x in window.start.split(":"))
    eh, em = (int(x) for x in window.end.split(":"))
    return sh * 60 + sm <= cur < eh * 60 + em


def in_schedule(now: datetime, schedule: ScheduleConfig) -> bool:
    """工作时间窗口判断：开关、工作日（isoweekday: 1=周一...7=周日）、时间窗口。"""
    if not schedule.enabled:
        return True
    if now.isoweekday() not in schedule.workdays:
        return False
    if not schedule.time_windows:
        return True  # 开了窗口限制但没配窗口 → 只限制工作日
    return any(_within_window(now, w) for w in schedule.time_windows)


def rand_interval(min_s: int, max_s: int) -> int:
    """随机间隔（§7.3 真随机）。"""
    return random.randint(min_s, max_s)


def sample_apis(
    apis: list[ApiDefinition], n_min: int, n_max: int, shuffle: bool
) -> list[ApiDefinition]:
    """从启用 API 中随机抽取并随机排序；数量上限不超过可用数。"""
    enabled = [a for a in apis if a.enabled]
    if not enabled:
        return []
    upper = min(n_max, len(enabled))
    lower = min(n_min, upper)
    count = random.randint(lower, upper)
    picked = random.sample(enabled, count)
    if shuffle:
        random.shuffle(picked)
    return picked


async def interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """可被停止事件打断的 sleep。返回 True 表示被打断。"""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
