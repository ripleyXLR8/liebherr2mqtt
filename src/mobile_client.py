"""Optional client for the Liebherr SmartDevice *mobile* API.

⚠️ EXPERIMENTAL. This talks to Liebherr's **internal** mobile API — the one the
official SmartDevice app uses — rather than the documented, officially provided
HomeAPI. It exists only to surface data the HomeAPI does not expose, chiefly the
appliance **notifications / alarms** (door-open alarm, temperature alarm, power
failure, air filter reminder).

It is off by default and completely independent of the HomeAPI path: the bridge
runs normally on the api-key without ever importing anything here unless the
mobile mode is explicitly enabled.

How it authenticates (all values observed in the app, confirmed against the
public OIDC discovery document at login.liebherr.com/.well-known/openid-configuration):

- OAuth 2.0 Authorization Code + PKCE (S256) against https://login.liebherr.com
- client_id  : mobileapps_hau_smartdevice_flutter
- redirect   : smartdevice://auth
- scope      : openid profile email offline_access  (offline_access → refresh token)

The interactive login is a one-off (run `... auth`); after that the refresh
token in the token file keeps the daemon authenticated on its own.

Caveats the user has accepted: this is an undocumented private API that can
change without notice, and replaying the app's OAuth client is more sensitive
towards Liebherr's terms than the officially issued HomeAPI key. It runs on the
account whose credentials complete the login.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

LOGGER = logging.getLogger("liebherr2mqtt.mobile")

# --- Defaults observed in the SmartDevice app / OIDC discovery -------------
DEFAULT_AUTH_BASE = "https://login.liebherr.com"
DEFAULT_API_BASE = "https://mobile-api.smartdevice.liebherr.com"
DEFAULT_CLIENT_ID = "mobileapps_hau_smartdevice_flutter"
DEFAULT_REDIRECT_URI = "smartdevice://auth"
DEFAULT_SCOPE = "openid profile email offline_access"

# Notification types the app knows about, mapped to a coarse category so a
# handful of stable MQTT entities cover the lot. Unknown types fall back to a
# generic "other" bucket rather than being dropped.
NOTIFICATION_CATEGORY = {
    "door_alarm": "door",
    "auto_door_overheat_alarm": "door",
    "auto_door_obstacle_alarm": "door",
    "upper_temperature_alarm": "temperature",
    "lower_temperature_alarm": "temperature",
    "upper_power_failure_alarm": "power",
    "lower_power_failure_alarm": "power",
    "air_filter_reminder": "air_filter",
}

CATEGORY_LABEL = {
    "door": "Alarme porte",
    "temperature": "Alarme température",
    "power": "Coupure de courant",
    "air_filter": "Rappel filtre à air",
    "other": "Autre alarme",
}


class MobileAuthError(Exception):
    """Authentication against login.liebherr.com failed."""


class MobileApiError(Exception):
    """A call to the mobile API failed."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) for a PKCE S256 exchange."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class MobileConfig:
    """Everything the mobile client needs, all overridable for staging/testing."""

    auth_base: str = DEFAULT_AUTH_BASE
    api_base: str = DEFAULT_API_BASE
    client_id: str = DEFAULT_CLIENT_ID
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scope: str = DEFAULT_SCOPE
    token_file: str = "/config/liebherr_mobile_token.json"
    poll_interval: int = 300

    @classmethod
    def from_configparser(cls, config: Any) -> "MobileConfig":
        g = lambda opt, default: (
            config.get("mobile", opt, fallback=default) if config.has_section("mobile") else default
        )
        return cls(
            auth_base=g("auth_base", DEFAULT_AUTH_BASE).rstrip("/"),
            api_base=g("api_base", DEFAULT_API_BASE).rstrip("/"),
            client_id=g("client_id", DEFAULT_CLIENT_ID),
            redirect_uri=g("redirect_uri", DEFAULT_REDIRECT_URI),
            scope=g("scope", DEFAULT_SCOPE),
            token_file=g("token_file", "/config/liebherr_mobile_token.json"),
            poll_interval=int(g("poll_interval", "300") or "300"),
        )


class TokenStore:
    """Reads and writes the OAuth token file, with 0600 permissions."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self._path.is_file():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as err:
            LOGGER.error("Token file unreadable (%s): %s", self._path, err)
            return None

    def save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)

    @property
    def exists(self) -> bool:
        return self._path.is_file()


class MobileClient:
    """OAuth token lifecycle + notification reads against the mobile API."""

    def __init__(self, config: MobileConfig, session: aiohttp.ClientSession) -> None:
        self._cfg = config
        self._session = session
        self._store = TokenStore(config.token_file)
        self._access_token: str | None = None
        self._access_expiry: float = 0.0

    # -- Interactive one-off login (PKCE) -----------------------------------

    def build_authorize_url(self, verifier: str, state: str) -> str:
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        params = {
            "client_id": self._cfg.client_id,
            "redirect_uri": self._cfg.redirect_uri,
            "response_type": "code",
            "scope": self._cfg.scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{self._cfg.auth_base}/connect/authorize?{urlencode(params)}"

    @staticmethod
    def extract_code(redirect_url: str) -> str:
        """Pull the authorization code out of the pasted redirect URL."""
        qs = parse_qs(urlparse(redirect_url.strip()).query)
        if "error" in qs:
            raise MobileAuthError(f"Authorization returned an error: {qs['error'][0]}")
        if "code" not in qs:
            raise MobileAuthError(
                "No 'code' in the redirect URL. Paste the full "
                "smartdevice://auth?... URL the browser tried to open."
            )
        return qs["code"][0]

    async def exchange_code(self, code: str, verifier: str) -> None:
        """Trade an authorization code for tokens and persist them."""
        data = {
            "grant_type": "authorization_code",
            "client_id": self._cfg.client_id,
            "code": code,
            "redirect_uri": self._cfg.redirect_uri,
            "code_verifier": verifier,
        }
        await self._token_request(data, context="authorization_code exchange")

    # -- Token maintenance --------------------------------------------------

    async def _token_request(self, data: dict[str, str], context: str) -> None:
        url = f"{self._cfg.auth_base}/connect/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        async with self._session.post(url, data=data, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise MobileAuthError(f"{context} failed: HTTP {resp.status} {body[:200]}")
            payload = json.loads(body)

        now = time.time()
        stored = self._store.load() or {}
        # IdentityServer rotates refresh tokens: keep the newest, fall back to
        # the previous one if a refresh response omits it.
        refresh = payload.get("refresh_token") or stored.get("refresh_token")
        record = {
            "refresh_token": refresh,
            "access_token": payload.get("access_token"),
            "expires_at": now + int(payload.get("expires_in", 3600)),
            "scope": payload.get("scope", self._cfg.scope),
            "obtained_at": now,
        }
        self._store.save(record)
        self._access_token = record["access_token"]
        self._access_expiry = record["expires_at"]
        LOGGER.info("Mobile token stored (%s), valid ~%ds", context, int(payload.get("expires_in", 0)))

    async def _refresh(self) -> None:
        stored = self._store.load()
        if not stored or not stored.get("refresh_token"):
            raise MobileAuthError(
                "No refresh token. Run the one-off login first ("
                "'... auth') to authorise the mobile API."
            )
        data = {
            "grant_type": "refresh_token",
            "client_id": self._cfg.client_id,
            "refresh_token": stored["refresh_token"],
        }
        await self._token_request(data, context="refresh_token")

    async def _valid_access_token(self) -> str:
        stored = self._store.load()
        if stored and self._access_token is None:
            self._access_token = stored.get("access_token")
            self._access_expiry = stored.get("expires_at", 0)
        # Refresh 60 s before expiry to avoid racing the clock.
        if not self._access_token or time.time() > self._access_expiry - 60:
            await self._refresh()
        assert self._access_token is not None
        return self._access_token

    @property
    def is_authorised(self) -> bool:
        return self._store.exists

    # -- API calls ----------------------------------------------------------

    async def _get(self, path: str) -> Any:
        token = await self._valid_access_token()
        url = f"{self._cfg.api_base}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with self._session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status == 401:
                # Access token might have just been revoked; one forced refresh.
                await self._refresh()
                headers["Authorization"] = f"Bearer {self._access_token}"
                async with self._session.get(url, headers=headers) as resp2:
                    body = await resp2.text()
                    if resp2.status != 200:
                        raise MobileApiError(f"GET {path}: HTTP {resp2.status} {body[:200]}")
                    return json.loads(body) if body else None
            if resp.status != 200:
                raise MobileApiError(f"GET {path}: HTTP {resp.status} {body[:200]}")
            return json.loads(body) if body else None

    async def get_appliances(self) -> list[dict[str, Any]]:
        data = await self._get("/v1/household/appliances")
        return data if isinstance(data, list) else data.get("appliances", []) if data else []

    async def get_notifications(self) -> list[dict[str, Any]]:
        data = await self._get("/v1/household/notifications")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("notifications") or data.get("items") or []
        return []


def summarise_notifications(
    notifications: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Reduce a raw notification list to per-(device, category) active flags.

    Returns {deviceId: {category: {"active": bool, "latest": {...}}}}.
    A category is active when at least one *unacknowledged* notification of a
    type mapping to it exists for that device.
    """
    result: dict[str, dict[str, Any]] = {}
    for note in notifications:
        device_id = note.get("deviceId") or note.get("applianceId") or "unknown"
        ntype = (note.get("notificationType") or note.get("type") or "").lower()
        category = NOTIFICATION_CATEGORY.get(ntype, "other")
        acknowledged = bool(note.get("isAcknowledged", note.get("acknowledged", False)))
        bucket = result.setdefault(device_id, {})
        cat = bucket.setdefault(category, {"active": False, "latest": None})
        if not acknowledged:
            cat["active"] = True
        # Track the most recent notification regardless of ack state.
        created = note.get("createdAt") or note.get("timestamp") or ""
        if cat["latest"] is None or created > (cat["latest"].get("createdAt") or ""):
            cat["latest"] = {
                "type": ntype,
                "createdAt": created,
                "acknowledged": acknowledged,
            }
    return result
