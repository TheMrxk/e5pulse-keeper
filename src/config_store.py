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

Hot-reloadable configuration (in-memory apply + write back to config.yaml with
a pre-write backup): account enabled flag, interval bounds, API toggles, schedule,
global settings, error handling, call policy, and account add/delete.
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
    """Raised when a hot-config change is invalid (out of range / restart-only / validation)."""


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
        self._persist(cfg, f"account[{account_name}] enabled={enabled}")
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
        self._persist(cfg, f"account[{account_name}] interval={min_interval}~{max_interval}")
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
                    f"account[{account_name}] has no private apis (currently inherits default_apis)"
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
        self._persist(cfg, "global schedule updated")
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
        self._persist(cfg, f"global default interval={min_interval}~{max_interval}s")
        return cfg

    def update_concurrency(self, max_concurrent: int) -> AppConfig:
        """最大并发账号数（1~20，apply_config 会据此重建信号量）。"""
        cfg = self._reload_lenient()
        cfg.global_.max_concurrent_accounts = max_concurrent  # Pydantic 边界在 _persist 兜底
        self._persist(cfg, f"max concurrency={max_concurrent}")
        return cfg

    def update_request_timeout(self, timeout: int) -> AppConfig:
        """单次 Graph 请求超时（5~120 秒）。"""
        cfg = self._reload_lenient()
        cfg.global_.request_timeout = timeout
        self._persist(cfg, f"request timeout={timeout}s")
        return cfg

    def update_error_handling(self, eh_dict: dict) -> AppConfig:
        """错误处理整块替换，借用 Pydantic 严格校验。"""
        from .models import ErrorHandlingConfig
        cfg = self._reload_lenient()
        cfg.error_handling = ErrorHandlingConfig.model_validate(eh_dict)
        self._persist(cfg, "error handling updated")
        return cfg

    def update_call_policy(self, policy_dict: dict) -> AppConfig:
        """调用策略整块替换，借用 Pydantic 严格校验。"""
        from .models import CallPolicy
        cfg = self._reload_lenient()
        cfg.call_policy = CallPolicy.model_validate(policy_dict)
        self._persist(cfg, "call policy updated")
        return cfg

    # ---------- 账号增删（Phase 4-⑥，热生效） ----------

    def add_account(self, account_dict: dict) -> AppConfig:
        """新增一个 CC 模式账号并写盘。

        凭证由调用方先做真实 token 验证；这里负责重名/重复检测与 Pydantic 校验。
        """
        from .models import AccountConfig
        cfg = self._reload_lenient()
        name = (account_dict.get("name") or "").strip()
        if not name:
            raise HotConfigError("account name must not be empty")
        # 重名检测
        if any(a.name == name for a in cfg.accounts):
            raise HotConfigError(f"account name already exists: {name}")
        # 同 tenant+client 重复检测（避免重复应用注册）
        for a in cfg.accounts:
            if (a.tenant_id == account_dict.get("tenant_id")
                    and a.client_id == account_dict.get("client_id")):
                raise HotConfigError(
                    f"an account with the same tenant_id+client_id already exists: {a.name}")
        # 强制 CC 模式，在线入口不接受 ROPC（兼容性差，仍需走文件配置）
        payload = dict(account_dict)
        payload["auth_type"] = "client_credentials"
        try:
            new_account = AccountConfig.model_validate(payload)
        except Exception as exc:
            raise HotConfigError(f"account validation failed, write rejected: {exc}") from exc
        cfg.accounts.append(new_account)
        self._persist(cfg, f"add account[{name}]")
        return cfg

    def delete_account(self, name: str) -> AppConfig:
        """按 name 删除账号；至少保留一个，防止配置变空导致无法启动。"""
        cfg = self._reload_lenient()
        before = len(cfg.accounts)
        cfg.accounts = [a for a in cfg.accounts if a.name != name]
        if len(cfg.accounts) == before:
            raise HotConfigError(f"account not found: {name}")
        if not cfg.accounts:
            raise HotConfigError("at least one account must remain; refusing to delete the last one")
        self._persist(cfg, f"delete account[{name}]")
        return cfg

    # ---------- 内部工具 ----------

    @staticmethod
    def _find_account(cfg: AppConfig, name: str):
        for account in cfg.accounts:
            if account.name == name:
                return account
        raise HotConfigError(f"account not found: {name}")

    def _persist(self, cfg: AppConfig, action: str) -> None:
        # 热改放宽"至少一个启用账号"的启动硬校验：用户可能主动停用全部账号，
        # 此时服务空转合法；其余结构/类型/边界校验仍然严格执行。
        try:
            dumped = cfg.model_dump(mode="json", by_alias=True, exclude_none=True)
            AppConfig.model_validate(dumped)
        except Exception as exc:
            if "no account has enabled=true" not in str(exc):
                raise HotConfigError(f"post-change config validation failed, write rejected: {exc}") from exc
            logger.warning("no enabled account after hot change, service will idle until re-enabled: %s", action)
        self._backup()
        self._write(cfg)
        logger.info("hot config applied: %s", action)
