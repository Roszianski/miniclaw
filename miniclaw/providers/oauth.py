"""OAuth device-flow adapters and token storage helpers."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class OAuthDeviceFlow:
    """Device-flow challenge values returned by provider authorization endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    interval: int = 5
    expires_in: int = 900


@dataclass
class OAuthTokenSet:
    """OAuth access/refresh token payload."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    scope: str | None = None
    expires_at: int | None = None

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        fallback_refresh_token: str | None = None,
    ) -> "OAuthTokenSet":
        expires_in = int(payload.get("expires_in") or 0)
        expires_at = int(time.time()) + expires_in if expires_in > 0 else None
        refresh = payload.get("refresh_token") or fallback_refresh_token
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(refresh) if refresh else None,
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=str(payload.get("scope")) if payload.get("scope") else None,
            expires_at=expires_at,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token or "",
            "token_type": self.token_type,
            "scope": self.scope or "",
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_stored_payload(cls, payload: dict[str, Any]) -> "OAuthTokenSet":
        expires_at = payload.get("expires_at")
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=(str(payload.get("refresh_token")) if payload.get("refresh_token") else None),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=(str(payload.get("scope")) if payload.get("scope") else None),
            expires_at=int(expires_at) if isinstance(expires_at, (int, float, str)) and str(expires_at).strip() else None,
        )

    def is_expired(self, *, skew_seconds: int = 30) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= int(time.time()) + max(0, skew_seconds)

    def seconds_remaining(self) -> int | None:
        if self.expires_at is None:
            return None
        return self.expires_at - int(time.time())


class OAuthDeviceFlowAdapter:
    """Provider-specific OAuth adapter backed by RFC 8628 device flow endpoints."""

    def __init__(
        self,
        *,
        provider: str,
        device_code_url: str,
        token_url: str,
        revoke_url: str | None = None,
        client_id: str = "miniclaw-cli",
        scope: str = "offline_access",
    ):
        self.provider = provider
        self.device_code_url = device_code_url
        self.token_url = token_url
        self.revoke_url = revoke_url
        self.client_id = client_id
        self.scope = scope

    def start_device_flow(self) -> OAuthDeviceFlow:
        payload, status = self._post_form(
            self.device_code_url,
            {"client_id": self.client_id, "scope": self.scope},
            allow_error=True,
        )
        if status >= 400:
            raise RuntimeError(self._error_message(payload, status, context="device code"))

        return OAuthDeviceFlow(
            device_code=str(payload.get("device_code") or ""),
            user_code=str(payload.get("user_code") or ""),
            verification_uri=str(payload.get("verification_uri") or ""),
            verification_uri_complete=(
                str(payload.get("verification_uri_complete"))
                if payload.get("verification_uri_complete")
                else None
            ),
            interval=int(payload.get("interval") or 5),
            expires_in=int(payload.get("expires_in") or 900),
        )

    def poll_for_token(self, flow: OAuthDeviceFlow) -> OAuthTokenSet:
        deadline = int(time.time()) + max(1, flow.expires_in)
        interval = max(1, int(flow.interval or 5))
        while int(time.time()) < deadline:
            payload, status = self._post_form(
                self.token_url,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": flow.device_code,
                    "client_id": self.client_id,
                },
                allow_error=True,
            )
            if status < 400:
                token = OAuthTokenSet.from_payload(payload)
                if not token.access_token:
                    raise RuntimeError(f"{self.provider} OAuth token response missing access_token.")
                return token

            error = str(payload.get("error") or "").strip().lower()
            if error == "authorization_pending":
                time.sleep(interval)
                continue
            if error == "slow_down":
                interval = min(interval + 2, 15)
                time.sleep(interval)
                continue
            if error == "expired_token":
                raise RuntimeError(f"{self.provider} OAuth device code expired before authorization completed.")
            raise RuntimeError(self._error_message(payload, status, context="device token exchange"))

        raise RuntimeError(f"{self.provider} OAuth login timed out before authorization completed.")

    def refresh_token(self, refresh_token: str) -> OAuthTokenSet:
        payload, status = self._post_form(
            self.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
            allow_error=True,
        )
        if status >= 400:
            raise RuntimeError(self._error_message(payload, status, context="refresh token exchange"))
        token = OAuthTokenSet.from_payload(payload, fallback_refresh_token=refresh_token)
        if not token.access_token:
            raise RuntimeError(f"{self.provider} OAuth refresh response missing access_token.")
        return token

    def revoke_token(self, token: str) -> bool:
        if not self.revoke_url:
            return False
        payload, status = self._post_form(
            self.revoke_url,
            {"client_id": self.client_id, "token": token},
            allow_error=True,
        )
        if status >= 400:
            raise RuntimeError(self._error_message(payload, status, context="token revoke"))
        return True

    def _post_form(
        self,
        url: str,
        form_data: dict[str, Any],
        *,
        allow_error: bool,
    ) -> tuple[dict[str, Any], int]:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                url,
                data=form_data,
                headers={"Accept": "application/json"},
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {"raw": payload}
        except Exception:
            payload = {"raw": response.text}

        if not allow_error and response.status_code >= 400:
            raise RuntimeError(self._error_message(payload, response.status_code))
        return payload, response.status_code

    def _error_message(self, payload: dict[str, Any], status: int, context: str = "request") -> str:
        error = str(payload.get("error") or "").strip()
        detail = str(payload.get("error_description") or payload.get("raw") or "").strip()
        if error and detail:
            return f"{self.provider} OAuth {context} failed ({status}): {error} - {detail}"
        if error:
            return f"{self.provider} OAuth {context} failed ({status}): {error}"
        if detail:
            return f"{self.provider} OAuth {context} failed ({status}): {detail}"
        return f"{self.provider} OAuth {context} failed ({status})."


def get_oauth_adapter(provider: str) -> OAuthDeviceFlowAdapter:
    """Get OAuth adapter for a supported provider."""
    normalized = provider.strip().lower()
    if normalized == "openai":
        return OAuthDeviceFlowAdapter(
            provider="openai",
            device_code_url=os.environ.get(
                "MINICLAW_OPENAI_OAUTH_DEVICE_CODE_URL",
                "https://auth.openai.com/oauth/device/code",
            ),
            token_url=os.environ.get(
                "MINICLAW_OPENAI_OAUTH_TOKEN_URL",
                "https://auth.openai.com/oauth/token",
            ),
            revoke_url=os.environ.get(
                "MINICLAW_OPENAI_OAUTH_REVOKE_URL",
                "https://auth.openai.com/oauth/revoke",
            ),
            client_id=os.environ.get("MINICLAW_OPENAI_OAUTH_CLIENT_ID", "miniclaw-cli"),
            scope=os.environ.get("MINICLAW_OPENAI_OAUTH_SCOPE", "offline_access"),
        )
    if normalized == "anthropic":
        return OAuthDeviceFlowAdapter(
            provider="anthropic",
            device_code_url=os.environ.get(
                "MINICLAW_ANTHROPIC_OAUTH_DEVICE_CODE_URL",
                "https://console.anthropic.com/oauth/device/code",
            ),
            token_url=os.environ.get(
                "MINICLAW_ANTHROPIC_OAUTH_TOKEN_URL",
                "https://console.anthropic.com/oauth/token",
            ),
            revoke_url=os.environ.get(
                "MINICLAW_ANTHROPIC_OAUTH_REVOKE_URL",
                "https://console.anthropic.com/oauth/revoke",
            ),
            client_id=os.environ.get("MINICLAW_ANTHROPIC_OAUTH_CLIENT_ID", "miniclaw-cli"),
            scope=os.environ.get("MINICLAW_ANTHROPIC_OAUTH_SCOPE", "offline_access"),
        )
    raise ValueError(f"Unsupported OAuth provider: {provider}")


def oauth_secret_key(provider: str) -> str:
    """Default namespaced secret key for OAuth token payload."""
    return f"oauth:{provider.strip().lower()}:token"


def resolve_oauth_token_ref(provider: str, configured_ref: str | None = None) -> str:
    """Resolve configured token ref or default namespaced key."""
    ref = (configured_ref or "").strip()
    return ref or oauth_secret_key(provider)


def load_token_from_store(secret_store, token_ref: str) -> OAuthTokenSet | None:
    """Load token payload from secret store."""
    raw = secret_store.get(token_ref)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        token = OAuthTokenSet.from_stored_payload(payload)
        if not token.access_token:
            return None
        return token
    except Exception:
        return None


def save_token_to_store(secret_store, token_ref: str, token: OAuthTokenSet) -> bool:
    """Persist token payload into secret store."""
    return bool(secret_store.set(token_ref, json.dumps(token.to_payload(), separators=(",", ":"))))


def delete_token_from_store(secret_store, token_ref: str) -> bool:
    """Delete token payload from secret store."""
    return bool(secret_store.delete(token_ref))

