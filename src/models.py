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

Pydantic 配置与状态模型（开发文档 §6.2 严格校验）。

启动时一次性完成全部校验，配置错误立即报错退出，绝不带病运行。
敏感字段统一 SecretStr，repr/日志天然脱敏（§13.4）。
"""

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class AuthType(str, enum.Enum):
    CLIENT_CREDENTIALS = "client_credentials"
    ROPC = "ropc"


class HttpMethod(str, enum.Enum):
    GET = "GET"
    POST = "POST"
    PATCH = "PATCH"
    PUT = "PUT"
    DELETE = "DELETE"


class ApiDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    method: HttpMethod = HttpMethod.GET
    url: str
    enabled: bool = True
    body: dict[str, Any] | None = None


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: str = "INFO"
    log_file: str = "/data/logs/e5pulse.log"
    max_concurrent_accounts: int = Field(default=3, ge=1, le=20)
    default_min_interval: int = Field(default=1200, ge=30)
    default_max_interval: int = Field(default=2400, ge=30)
    timezone: str = "Asia/Shanghai"
    request_timeout: int = Field(default=30, ge=5, le=120)
    state_file: str = "/data/status.json"

    @model_validator(mode="after")
    def _check_interval(self) -> "GlobalConfig":
        if self.default_min_interval > self.default_max_interval:
            raise ValueError(
                f"global.default_min_interval({self.default_min_interval}) "
                f"cannot exceed default_max_interval({self.default_max_interval})"
            )
        return self


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")

    @model_validator(mode="after")
    def _check_window(self) -> "TimeWindow":
        sh, sm = (int(x) for x in self.start.split(":"))
        eh, em = (int(x) for x in self.end.split(":"))
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
            raise ValueError(f"Time window out of range: {self.start}~{self.end}")
        if sh * 60 + sm >= eh * 60 + em:
            raise ValueError(f"Time window start must be earlier than end: {self.start}~{self.end}")
        return self


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    workdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    time_windows: list[TimeWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_workdays(self) -> "ScheduleConfig":
        if any(d not in range(1, 8) for d in self.workdays):
            raise ValueError(f"workdays values must be in 1~7 (1=Monday): {self.workdays}")
        if len(set(self.workdays)) != len(self.workdays):
            raise ValueError(f"Duplicate workdays: {self.workdays}")
        return self


class ErrorHandlingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_consecutive_failures: int = Field(default=8, ge=1)
    pause_minutes: int = Field(default=180, ge=1)
    max_error_rate: float = Field(default=0.6, gt=0.0, le=1.0)
    sliding_window_size: int = Field(default=20, ge=2)
    retry_times: int = Field(default=3, ge=0, le=6)
    retry_backoff: list[int] = Field(default_factory=lambda: [2, 5, 10])

    @model_validator(mode="after")
    def _check_backoff(self) -> "ErrorHandlingConfig":
        if any(b < 0 for b in self.retry_backoff):
            raise ValueError("retry_backoff must not contain negative values")
        return self


class CallPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apis_per_round_min: int = Field(default=1, ge=1)
    apis_per_round_max: int = Field(default=3, ge=1)
    shuffle_order: bool = True
    inter_call_delay_min: int = Field(default=1, ge=0)
    inter_call_delay_max: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "CallPolicy":
        if self.apis_per_round_min > self.apis_per_round_max:
            raise ValueError("apis_per_round_min cannot exceed apis_per_round_max")
        if self.inter_call_delay_min > self.inter_call_delay_max:
            raise ValueError("inter_call_delay_min cannot exceed inter_call_delay_max")
        return self


# 面板相关配置（Phase 4 使用，Phase 1 先占位，保证配置可写）
class PanelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8090, ge=1, le=65535)
    password: SecretStr | None = None
    session_secret: SecretStr | None = None
    login_fail_limit: int = Field(default=5, ge=1)


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    auth_type: AuthType
    tenant_id: str
    client_id: str
    client_secret: SecretStr
    # ROPC 专用
    username: str | None = None
    password: SecretStr | None = None
    # 账号级覆盖
    min_interval: int | None = Field(default=None, ge=30)
    max_interval: int | None = Field(default=None, ge=30)
    schedule: ScheduleConfig | None = None
    apis: list[ApiDefinition] | None = None

    @model_validator(mode="after")
    def _check_auth_and_interval(self) -> "AccountConfig":
        if self.auth_type is AuthType.ROPC:
            if not self.username or not self.password:
                raise ValueError(
                    f"账号[{self.name}] auth_type=ropc 时必须提供 username 和 password"
                )
        if self.min_interval is not None and self.max_interval is not None:
            if self.min_interval > self.max_interval:
                raise ValueError(
                    f"账号[{self.name}] min_interval({self.min_interval}) "
                    f"不能大于 max_interval({self.max_interval})"
                )
        return self

    def effective_interval(self, glob: GlobalConfig) -> tuple[int, int]:
        """账号级覆盖回退全局默认。"""
        lo = self.min_interval if self.min_interval is not None else glob.default_min_interval
        hi = self.max_interval if self.max_interval is not None else glob.default_max_interval
        return lo, hi

    def effective_apis(self, default_apis: list[ApiDefinition]) -> list[ApiDefinition]:
        """账号 apis 为空则继承 default_apis。"""
        return list(self.apis) if self.apis is not None else list(default_apis)

    def effective_schedule(self, default_schedule: ScheduleConfig) -> ScheduleConfig:
        return self.schedule if self.schedule is not None else default_schedule


class AppConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    global_: GlobalConfig = Field(alias="global")
    schedule: ScheduleConfig
    error_handling: ErrorHandlingConfig = Field(default_factory=ErrorHandlingConfig)
    call_policy: CallPolicy = Field(default_factory=CallPolicy)
    panel: PanelConfig = Field(default_factory=PanelConfig)
    default_apis: list[ApiDefinition] = Field(default_factory=list)
    accounts: list[AccountConfig]

    @model_validator(mode="after")
    def _check_accounts(self) -> "AppConfig":
        if not self.accounts:
            raise ValueError("at least one account is required in accounts")
        names = [a.name for a in self.accounts]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate account names: {names}")
        if not any(a.enabled for a in self.accounts):
            raise ValueError("no account has enabled=true")
        if not any(api.enabled for api in self.default_apis):
            raise ValueError("no API in default_apis has enabled=true")
        return self
