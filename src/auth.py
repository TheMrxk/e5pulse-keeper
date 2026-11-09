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

MSAL 认证封装（开发文档 §4）。

- Client Credentials：ConfidentialClientApplication，应用权限，无用户上下文
- ROPC：PublicClientApplication + 账号密码，委托权限（§4.2，兼容性差）
- MSAL 是同步库，统一 asyncio.to_thread 包裹，避免阻塞事件循环
- 错误二分：AuthConfigError（凭证/配置，重试无意义）/ AuthTransientError（可重试）
"""

import asyncio
import logging

import msal

from .models import AccountConfig, AuthType

logger = logging.getLogger("e5pulse")

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
_AUTHORITY = "https://login.microsoftonline.com/{tenant}"

# 微软身份平台确定性错误 —— 重试无意义，属配置/凭证问题
# error 字段（字符串标识）
_FATAL_ERROR_IDS = {
    "invalid_client",
    "unauthorized_client",
    "invalid_grant",
    "consent_required",
    "invalid_tenant",
    "interaction_required",       # ROPC 撞上 MFA/条件访问，必须交互式登录，重试无用
    "invalid_username_password",
}
# error_codes 字段（数字码），常见 AADSTS 码
# 50076=需 MFA 多因素  65001=无权限/未同意  50020=用户不在租户  70011=无效 scope
_FATAL_NUMERIC_CODES = {50076, 65001, 50020, 70011, 700016}


class AuthError(Exception):
    """认证错误基类。"""


class AuthConfigError(AuthError):
    """凭证/配置类错误，重试无意义，应触发账号暂停。"""


class AuthTransientError(AuthError):
    """网络/服务临时错误，可重试。"""


class AuthProvider:
    def __init__(self, account: AccountConfig) -> None:
        self._account = account
        self._app = None  # msal.ClientApplication
        self._invalidated = True

    def _build_app(self):
        # instance_discovery=False：跳过 OIDC 实例发现（msal 1.39 正确参数，
        # 老的 validate_authority=False 已挡不住构造期发现）。标准权威地址 +
        # 自填租户 GUID，关闭发现既省一次启动请求，也让离线/受限网络可运行。
        authority = _AUTHORITY.format(tenant=self._account.tenant_id)
        if self._account.auth_type is AuthType.ROPC:
            # ROPC 是公共客户端流：不带 client_secret
            return msal.PublicClientApplication(
                client_id=self._account.client_id,
                authority=authority,
                instance_discovery=False,
            )
        return msal.ConfidentialClientApplication(
            client_id=self._account.client_id,
            client_credential=self._account.client_secret.get_secret_value(),
            authority=authority,
            instance_discovery=False,
        )

    async def get_token(self) -> str:
        """返回有效 access token；失败抛 AuthConfigError / AuthTransientError。"""
        if self._app is None or self._invalidated:
            self._app = self._build_app()
            self._invalidated = False

        if self._account.auth_type is AuthType.ROPC:
            result = await asyncio.to_thread(self._acquire_ropc)
        else:
            result = await asyncio.to_thread(self._acquire_cc)

        if "access_token" in result:
            return str(result["access_token"])
        raise self._classify_error(result)

    def _acquire_cc(self) -> dict:
        # MSAL 内部 token cache 自动复用未过期 token 并临期刷新（§4.3）
        result = self._app.acquire_token_silent(_GRAPH_SCOPE, account=None)
        if result:
            return result
        return self._app.acquire_token_for_client(scopes=_GRAPH_SCOPE)

    def _acquire_ropc(self) -> dict:
        assert self._account.username is not None
        assert self._account.password is not None
        # ROPC 的 silent 复用：MSAL 对公共客户端按账号缓存，先用缓存
        accounts = self._app.get_accounts()
        if accounts:
            result = self._app.acquire_token_silent(
                _GRAPH_SCOPE, account=accounts[0]
            )
            if result:
                return result
        return self._app.acquire_token_by_username_password(
            username=self._account.username,
            password=self._account.password.get_secret_value(),
            scopes=_GRAPH_SCOPE,
        )

    def _classify_error(self, result: dict) -> AuthError:
        error_id = str(result.get("error") or "")
        err_codes = result.get("error_codes") or []
        # error_description 可能很长且含冗余 trace，截断即可
        desc = str(result.get("error_description") or "").split("\r\n")[0][:200]

        is_fatal = (
            error_id in _FATAL_ERROR_IDS
            or any(int(c) in _FATAL_NUMERIC_CODES for c in err_codes
                   if str(c).isdigit())
        )
        prefix = "configuration" if is_fatal else "临时"
        detail = f"{error_id or 'unknown'} codes={err_codes} {desc}".strip()
        if is_fatal:
            return AuthConfigError(f"Authentication failed ({prefix}): {detail}")
        return AuthTransientError(f"Authentication failed ({prefix}): {detail}")

    def invalidate(self) -> None:
        """401/403 后调用，强制下一轮重新走完整认证流程。"""
        self._invalidated = True
        logger.warning("[%s] token 已标记失效，下次调用强制重新获取", self._account.name)
