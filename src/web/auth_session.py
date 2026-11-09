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

面板会话鉴权（开发文档 §12.1、§13）。

- 密码存配置（SecretStr），签名 cookie 承载会话，itsdangerous 防篡改
- 登录失败按 IP 限流，超阈值冷却，防爆破
- 不引入数据库/Redis，内存计数即可（单容器单进程足够）
"""

import logging
import secrets
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger("e5pulse")

COOKIE_NAME = "e5pulse_session"
_SESSION_MAX_AGE = 12 * 3600  # 12 小时


@dataclass
class _FailRecord:
    count: int = 0
    locked_until: float = 0.0


class SessionAuth:
    def __init__(
        self,
        password: str,
        session_secret: str | None,
        fail_limit: int = 5,
        lock_seconds: int = 300,
    ) -> None:
        self._password = password
        # 未配置 session_secret 则每次启动随机生成（重启后旧 cookie 失效，安全可接受）
        secret = session_secret or secrets.token_urlsafe(48)
        self._serializer = URLSafeTimedSerializer(secret, salt="e5pulse-panel")
        self._fail_limit = fail_limit
        self._lock_seconds = lock_seconds
        self._records: dict[str, _FailRecord] = {}

    def _record(self, ip: str) -> _FailRecord:
        rec = self._records.get(ip)
        if rec is None:
            rec = _FailRecord()
            self._records[ip] = rec
        return rec

    def is_locked(self, ip: str) -> tuple[bool, int]:
        """返回(是否锁定, 剩余锁定秒数)。"""
        rec = self._records.get(ip)
        if rec and rec.locked_until > time.time():
            return True, int(rec.locked_until - time.time())
        return False, 0

    def verify_password(self, ip: str, candidate: str) -> bool:
        locked, wait = self.is_locked(ip)
        if locked:
            logger.warning("面板登录被限流，IP=%s 还需 %ds", ip, wait)
            return False
        # 恒定时间比较，防时序侧信道
        ok = secrets.compare_digest(candidate, self._password)
        rec = self._record(ip)
        if ok:
            rec.count = 0
            rec.locked_until = 0.0
            logger.info("面板登录成功 IP=%s", ip)
        else:
            rec.count += 1
            logger.warning("面板登录失败 IP=%s 第 %d 次", ip, rec.count)
            if rec.count >= self._fail_limit:
                rec.locked_until = time.time() + self._lock_seconds
                logger.error("面板登录失败达阈值，IP=%s 锁定 %ds", ip, self._lock_seconds)
        return ok

    def issue_cookie(self) -> str:
        return self._serializer.dumps({"ok": True})

    def validate_cookie(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=_SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return False
        return bool(data.get("ok"))
