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

面板热改配置（开发文档 §12.1 热改边界铁律）。

可热改（内存即时生效 + 写回 config.yaml，写前自动备份）：
- 账号 enabled 启停、间隔上下限、API 启用开关、工作时间窗口
必须重启（面板拒绝在线修改）：
- 账号增删、client_secret/认证类型/凭证、max_concurrent、日志路径等
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from .config import load_config
from .models import AppConfig

logger = logging.getLogger("e5pulse")


class HotConfigError(Exception):
    """热改非法（越界/需重启/校验失败）。"""


def dump_config_with_secrets(cfg: AppConfig) -> dict:
    """把 AppConfig 序列化成可写回 YAML 的 dict，并还原 SecretStr 明文。

    pydantic v2 的 SecretStr 在 model_dump(mode='json') 下会输出 '**********'，
    直接写盘会永久毁掉密钥，故这里统一从模型对象用 get_secret_value() 还原。
    调用方需保证落盘文件权限 600。
    """
    data = cfg.model_dump(mode="json", by_alias=True, exclude_none=True)
    panel = data.get("panel", {})
    if cfg.panel.password is not None:
        panel["password"] = cfg.panel.password.get_secret_value()
    if cfg.panel.session_secret is not None:
        panel["session_secret"] = cfg.panel.session_secret.get_secret_value()
    for raw_acc, model_acc in zip(data.get("accounts", []), cfg.accounts):
        if model_acc.client_secret is not None:
            raw_acc["client_secret"] = model_acc.client_secret.get_secret_value()
        if model_acc.password is not None:
            raw_acc["password"] = model_acc.password.get_secret_value()
    return data


class ConfigStore:
    """封装 config.yaml 的备份、写回与重载校验。"""

    def __init__(self, config_path: str) -> None:
        self._path = Path(config_path)

    def _backup_dir(self) -> Path:
        """备份目录：优先 /data/backups（容器内可写），回退配置文件同级。

        config.yaml 挂在 /app（root 所有，容器用户不可写），不能把备份放同级；
        /data 是挂载卷且对运行用户可写，故备份统一落 /data/backups。
        """
        data_dir = Path("/data")
        if data_dir.is_dir() and os.access(data_dir, os.W_OK):
            backup_dir = data_dir / "backups"
        else:
            backup_dir = self._path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        return backup_dir

    def _backup(self) -> Path:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._backup_dir() / f"config-backup-{ts}.yaml"
        shutil.copy2(self._path, backup)
        logger.info("配置写回前已备份: %s", backup)
        return backup

    def _write(self, cfg: AppConfig) -> None:
        # 关键：pydantic v2 的 SecretStr 在 model_dump(mode='json') 下会被
        # 序列化成字面量 '**********'，直接写盘会永久毁掉密钥。
        data = dump_config_with_secrets(cfg)
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def reload(self) -> AppConfig:
        """从磁盘重新加载并完整校验。"""
        return load_config(self._path)

    def _reload_lenient(self) -> AppConfig:
        """热改链路专用：允许'无启用账号'（用户主动全停用），其余严格校验。"""
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        # 先确保至少一个启用以通过启动级校验，读出后再按磁盘真实值还原
        accounts = raw.get("accounts", [])
        any_enabled = any(a.get("enabled", True) for a in accounts)
        if not any_enabled and accounts:
            accounts[0] = {**accounts[0], "enabled": True}
            cfg = AppConfig.model_validate(raw)
            for model_acc, raw_acc in zip(cfg.accounts, raw["accounts"]):
                model_acc.enabled = raw_acc.get("enabled", True)
            return cfg
        return AppConfig.model_validate(raw)

    # ---------- 可热改项 ----------

    def update_account_enabled(self, account_name: str, enabled: bool) -> AppConfig:
        cfg = self._reload_lenient()
        account = self._find_account(cfg, account_name)
        if account.enabled == enabled:
            return cfg  # 无变化，不写盘
        account.enabled = enabled
        self._persist(cfg, f"账号[{account_name}] enabled={enabled}")
        return cfg

    def update_account_interval(
        self, account_name: str, min_interval: int, max_interval: int
    ) -> AppConfig:
        cfg = self._reload_lenient()
        account = self._find_account(cfg, account_name)
        if min_interval > max_interval:
            raise HotConfigError(
                f"min_interval({min_interval}) cannot exceed max_interval({max_interval})"
            )
        account.min_interval = min_interval
        account.max_interval = max_interval
        self._persist(cfg, f"账号[{account_name}] 间隔={min_interval}~{max_interval}")
        return cfg

    def update_api_enabled(
        self, api_name: str, enabled: bool, account_name: str | None = None
    ) -> AppConfig:
        """切换 default_apis 或某账号私有 apis 里的 API 开关。"""
        cfg = self._reload_lenient()
        changed = False
        if account_name:
            account = self._find_account(cfg, account_name)
            if not account.apis:
                raise HotConfigError(
                    f"账号[{account_name}] 没有私有 apis（当前继承 default_apis）"
                )
            for api in account.apis:
                if api.name == api_name:
                    api.enabled = enabled
                    changed = True
        else:
            for api in cfg.default_apis:
                if api.name == api_name:
                    api.enabled = enabled
                    changed = True
        if not changed:
            raise HotConfigError(f"API not found: {api_name}")
        self._persist(cfg, f"API[{api_name}] enabled={enabled}")
        return cfg

    def update_schedule(self, schedule_dict: dict) -> AppConfig:
        cfg = self._reload_lenient()
        # 借用 Pydantic 校验新 schedule，非法直接抛 ValidationError
        from .models import ScheduleConfig
        cfg.schedule = ScheduleConfig.model_validate(schedule_dict)
        self._persist(cfg, "全局工作时间窗口更新")
        return cfg

    # ---------- 全局默认 / 弹性 / 调用策略（Phase 4-⑤ 新增） ----------

    def update_global_interval(self, min_interval: int, max_interval: int) -> AppConfig:
        """全局默认调用间隔上下限（秒）。"""
        cfg = self._reload_lenient()
        if min_interval > max_interval:
            raise HotConfigError(
                f"default_min_interval({min_interval}) cannot exceed "
                f"default_max_interval({max_interval})"
            )
        cfg.global_.default_min_interval = min_interval
        cfg.global_.default_max_interval = max_interval
        self._persist(cfg, f"全局默认间隔={min_interval}~{max_interval}秒")
        return cfg

    def update_concurrency(self, max_concurrent: int) -> AppConfig:
        """最大并发账号数（1~20，apply_config 会据此重建信号量）。"""
        cfg = self._reload_lenient()
        cfg.global_.max_concurrent_accounts = max_concurrent  # Pydantic 边界在 _persist 兜底
        self._persist(cfg, f"最大并发={max_concurrent}")
        return cfg

    def update_request_timeout(self, timeout: int) -> AppConfig:
        """单次 Graph 请求超时（5~120 秒）。"""
        cfg = self._reload_lenient()
        cfg.global_.request_timeout = timeout
        self._persist(cfg, f"请求超时={timeout}秒")
        return cfg

    def update_error_handling(self, eh_dict: dict) -> AppConfig:
        """错误处理整块替换，借用 Pydantic 严格校验。"""
        from .models import ErrorHandlingConfig
        cfg = self._reload_lenient()
        cfg.error_handling = ErrorHandlingConfig.model_validate(eh_dict)
        self._persist(cfg, "错误处理策略更新")
        return cfg

    def update_call_policy(self, policy_dict: dict) -> AppConfig:
        """调用策略整块替换，借用 Pydantic 严格校验。"""
        from .models import CallPolicy
        cfg = self._reload_lenient()
        cfg.call_policy = CallPolicy.model_validate(policy_dict)
        self._persist(cfg, "调用策略更新")
        return cfg

    # ---------- 内部工具 ----------

    @staticmethod
    def _find_account(cfg: AppConfig, name: str):
        for account in cfg.accounts:
            if account.name == name:
                return account
        raise HotConfigError(f"Account not found: {name}")

    def _persist(self, cfg: AppConfig, action: str) -> None:
        # 热改放宽"至少一个启用账号"的启动硬校验：用户可能主动停用全部账号，
        # 此时服务空转合法；其余结构/类型/边界校验仍然严格执行。
        try:
            dumped = cfg.model_dump(mode="json", by_alias=True, exclude_none=True)
            AppConfig.model_validate(dumped)
        except Exception as exc:
            if "no account has enabled=true" not in str(exc):
                raise HotConfigError(f"Post-change config validation failed, write rejected: {exc}") from exc
            logger.warning("热改后无启用账号，服务将空转等待重新启用: %s", action)
        self._backup()
        self._write(cfg)
        logger.info("热改成功: %s", action)
