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

YAML 配置加载（开发文档 §6.2）。

- 默认读取 /app/config.yaml（容器内），本地可用 E5PULSE_CONFIG 或命令行覆盖
- Pydantic 校验失败抛 ConfigError，由入口统一 exit(2)
"""

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import AppConfig

DEFAULT_CONFIG_PATH = "/app/config.yaml"


class ConfigError(Exception):
    """配置文件缺失或校验失败。"""


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    config_path = Path(
        path
        or os.environ.get("E5PULSE_CONFIG")
        or DEFAULT_CONFIG_PATH
    ).expanduser()

    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML syntax error: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a YAML mapping (key: value structure)")

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        # 错误逐条换行，启动日志里一眼能看出哪个字段出问题
        details = "\n".join(
            f"  - {'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError(f"Config validation failed:\n{details}") from exc
