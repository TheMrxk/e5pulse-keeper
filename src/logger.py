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

日志配置（开发文档 §9）。

- 控制台 + RotatingFileHandler（10MB × 5）
- 脱敏过滤器：Bearer token、client_secret、password 等只留前后 4 位
- 本地调试时 data/logs 目录不存在会自动创建
"""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "e5pulse"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

# Bearer eyJxxx...  /  "client_secret": "xxx"
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]+)")
_KV_SECRET_RE = re.compile(
    r"((?:client_secret|password|secret|token)[\"']?\s*[:=]\s*[\"']?)([^\s\"',&]+)",
    re.IGNORECASE,
)


def mask_value(value: str) -> str:
    """脱敏：只留前后 4 位，长度不足 8 全部打码。"""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


class SecretRedactFilter(logging.Filter):
    """日志输出前统一脱敏，防止 secret/token 落盘（§13.4）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = _BEARER_RE.sub(
            lambda m: m.group(1) + mask_value(m.group(2)), msg
        )
        redacted = _KV_SECRET_RE.sub(
            lambda m: m.group(1) + mask_value(m.group(2)), redacted
        )
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str, log_file: str) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    )
    redact = SecretRedactFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redact)
    logger.addHandler(console)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redact)
    logger.addHandler(file_handler)

    return logger
