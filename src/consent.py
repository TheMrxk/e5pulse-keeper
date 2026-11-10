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
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

import httpx
import msal

logger = logging.getLogger("e5pulse")

GRAPH_RESOURCE_APP_ID = "00000003-0000-0000-c000-000000000000"  # Fixed app ID of Microsoft Graph
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEVICE_CODE_TIMEOUT = 900  # Device code is valid for 15 minutes by default

# Allowlist of read-only Application roles grantable by the wizard (Graph appRole value).
# All are Read.All; no write permission is ever added to this set.
GRANTABLE_READONLY_ROLES = [
    "User.Read.All",
    "Directory.Read.All",
    "Group.Read.All",
    "Sites.Read.All",
    "Device.Read.All",
    "Application.Read.All",
    "AuditLog.Read.All",
]

# Delegated scopes for the device-code flow: read app roles and assign them.
# High-privilege delegated scopes of the signed-in admin, used only for this grant.
# NOTE: device-code scopes cannot mix reserved values such as offline_access/openid/profile;
# Microsoft rejects them (reserved-scope error), so only Graph resource scopes are used.
_ADMIN_SCOPES = [
    "https://graph.microsoft.com/AppRoleAssignment.ReadWrite.All",
    "https://graph.microsoft.com/Application.Read.All",
    "https://graph.microsoft.com/Directory.Read.All",
]


@dataclass
class ConsentSession:
    """In-memory state of one device-code consent session."""

    session_id: str
    tenant_id: str
    client_id: str
    user_code: str
    device_code: str
    verification_uri: str
    expires_at: float
    # pending / done / error
    status: str = "pending"
    message: str = ""
    granted_roles: list[str] = field(default_factory=list)
    admin_upn: str | None = None
    _flow: dict | None = None
    _app: object | None = None


class ConsentManager:
    """Manages device-code sessions and grants; all state stays in memory."""

    def __init__(self) -> None:
        self._sessions: dict[str, ConsentSession] = {}

    def start_session(self, tenant_id: str, client_id: str) -> ConsentSession:
        """Start a device-code flow and return the code and URL for admin sign-in."""
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        # Public client: device flow needs no client_secret
        app = msal.PublicClientApplication(
            client_id=client_id,
            authority=authority,
            instance_discovery=False,
        )
        flow = app.initiate_device_flow(scopes=_ADMIN_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(
                f"failed to initiate device flow: {flow.get('error_description') or flow}"
            )
        session = ConsentSession(
            session_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            client_id=client_id,
            user_code=flow["user_code"],
            device_code=flow["device_code"],
            verification_uri=flow.get("verification_uri", "https://microsoft.com/devicelogin"),
            expires_at=time.time() + _DEVICE_CODE_TIMEOUT,
            _flow=flow,
            _app=app,
        )
        self._sessions[session.session_id] = session
        logger.info("consent session started tenant=%s session=%s", tenant_id, session.session_id)
        return session

    def get_session(self, session_id: str) -> ConsentSession | None:
        s = self._sessions.get(session_id)
        if s and s.expires_at < time.time() and s.status == "pending":
            s.status = "error"
            s.message = "device code expired"
        return s

    async def poll_and_grant(
        self, session_id: str, target_client_id: str, roles: list[str]
    ) -> ConsentSession:
        """Poll the device-code sign-in in the background, then grant read-only roles.

        target_client_id: the app being granted (typically the keep-alive app).
        roles: requested appRole values, intersected with the allowlist.
        """
        session = self._sessions[session_id]
        try:
            # MSAL blocks; run it in a thread to keep the event loop free
            result = await asyncio.to_thread(
                session._app.acquire_token_by_device_flow, session._flow
            )
            if "access_token" not in result:
                session.status = "error"
                session.message = (
                    result.get("error_description")
                    or result.get("error")
                    or "admin sign-in failed"
                )
                return session

            token = result["access_token"]
            session.admin_upn = result.get("id_token_claims", {}).get("preferred_username")
            wanted = [r for r in roles if r in GRANTABLE_READONLY_ROLES]
            if not wanted:
                session.status = "error"
                session.message = "no grantable read-only role in request"
                return session

            granted = await self._assign_roles(token, target_client_id, wanted)
            session.granted_roles = granted
            session.status = "done"
            session.message = f"granted {len(granted)} role(s)"
            logger.info(
                "consent done admin=%s target=%s roles=%s",
                session.admin_upn, target_client_id, granted,
            )
        except Exception as exc:  # Converge any exception into a session error; never leak a stack trace
            session.status = "error"
            session.message = str(exc)[:300]
            logger.warning("consent session failed: %s", exc)
        finally:
            # Drop the token: keep only non-sensitive results, never the access token
            session._app = None
            session._flow = None
        return session

    async def _assign_roles(self, token: str, target_client_id: str, roles: list[str]) -> list[str]:
        """Assign Graph app roles to the target app service principal using the admin token."""
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Get the Graph service principal and its AppRoles
            graph_sp = await self._get_graph_service_principal(client, headers)
            role_map = {
                ar["value"]: ar["id"]
                for ar in graph_sp.get("appRoles", [])
                if ar.get("allowedMemberTypes") and "Application" in ar["allowedMemberTypes"]
            }
            # 2. Find the target app's service principal in this tenant
            target_sp = await self._get_target_service_principal(client, headers, target_client_id)

            # 3. Existing assignments, to avoid granting twice
            existing = await client.get(
                f"{_GRAPH_BASE}/servicePrincipals/{target_sp['id']}/appRoleAssignments",
                headers=headers,
            )
            existing.raise_for_status()
            existing_role_ids = {
                item["appRoleId"] for item in existing.json().get("value", [])
            }

            granted: list[str] = []
            for value in roles:
                role_id = role_map.get(value)
                if not role_id:
                    logger.info("role not found on Graph SP, skip: %s", value)
                    continue
                if role_id in existing_role_ids:
                    granted.append(value)  # Already granted counts as success
                    continue
                payload = {
                    "principalId": target_sp["id"],
                    "resourceId": graph_sp["id"],
                    "appRoleId": role_id,
                }
                resp = await client.post(
                    f"{_GRAPH_BASE}/servicePrincipals/{target_sp['id']}/appRoleAssignments",
                    headers=headers, json=payload,
                )
                if resp.status_code in (200, 201):
                    granted.append(value)
                else:
                    logger.warning("assign %s failed: %s", value, resp.text[:200])
            return granted

    async def _get_graph_service_principal(self, client: httpx.AsyncClient, headers: dict) -> dict:
        r = await client.get(
            f"{_GRAPH_BASE}/servicePrincipals?$filter=appId eq '{GRAPH_RESOURCE_APP_ID}'",
            headers=headers,
        )
        r.raise_for_status()
        value = r.json().get("value", [])
        if not value:
            raise RuntimeError("Microsoft Graph service principal not found in tenant")
        return value[0]

    async def _get_target_service_principal(
        self, client: httpx.AsyncClient, headers: dict, target_client_id: str
    ) -> dict:
        r = await client.get(
            f"{_GRAPH_BASE}/servicePrincipals?$filter=appId eq '{target_client_id}'",
            headers=headers,
        )
        r.raise_for_status()
        value = r.json().get("value", [])
        if not value:
            # Registration exists but its SP is not instantiated until first consent
            raise RuntimeError(
                "service principal for target app not found; complete an initial admin consent first"
            )
        return value[0]
