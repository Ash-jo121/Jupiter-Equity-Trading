from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from threading import RLock
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .research_store import IST, ResearchStore

AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
CREDENTIAL_NAME = "upstox_access_token"
# Upstox access tokens expire daily at 03:30 IST regardless of when issued.
TOKEN_EXPIRY = time(3, 30)


class UpstoxAuthError(RuntimeError):
    pass


def _token_valid_until(issued: datetime) -> datetime:
    """The next 03:30 IST at or after the issue time - when this token dies."""

    issued_ist = issued.astimezone(IST)
    expiry = issued_ist.replace(
        hour=TOKEN_EXPIRY.hour, minute=TOKEN_EXPIRY.minute, second=0, microsecond=0
    )
    if issued_ist >= expiry:
        expiry += timedelta(days=1)
    return expiry


class UpstoxTokenStore:
    """Select the live Upstox token without exposing it to strategy code.

    A read-only Analytics Token is preferred for this paper-trading application:
    it is long-lived and can supply market data without a daily OAuth login. When
    one is not configured, the store falls back to the standard daily access-token
    flow and persists exchanged tokens in the research database.

    The analytics token deliberately remains environment-owned rather than being
    copied into SQLite. A standard env-var token seeds the store on first boot; a
    token captured via OAuth then supersedes that seed.
    """

    def __init__(
        self,
        store: ResearchStore,
        api_key: str = "",
        api_secret: str = "",
        redirect_uri: str = "",
        env_token: str = "",
        analytics_token: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.store = store
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri
        self._analytics_token = analytics_token
        self._timeout = timeout
        self._lock = RLock()
        if env_token and self.store.get_credential(CREDENTIAL_NAME) is None:
            self.store.set_credential(CREDENTIAL_NAME, env_token)

    def current_token(self) -> str:
        if self._analytics_token:
            return self._analytics_token
        with self._lock:
            record = self.store.get_credential(CREDENTIAL_NAME)
        return record[0] if record else ""

    def set_token(self, token: str) -> None:
        if not token:
            raise UpstoxAuthError("token cannot be empty")
        with self._lock:
            self.store.set_credential(CREDENTIAL_NAME, token)

    @property
    def configured_for_oauth(self) -> bool:
        return bool(self.api_key and self.api_secret and self.redirect_uri)

    def authorization_url(self, state: Optional[str] = None) -> str:
        """The Upstox login link to open each morning."""

        if not self.configured_for_oauth:
            raise UpstoxAuthError(
                "set UPSTOX_API_KEY, UPSTOX_API_SECRET and UPSTOX_REDIRECT_URI to use OAuth"
            )
        params = {
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        return AUTH_DIALOG_URL + "?" + urlencode(params)

    def exchange_code(self, code: str) -> dict:
        """Trade the auth code from the redirect for a token, and store it."""

        if not code:
            raise UpstoxAuthError("authorization code is required")
        if not self.configured_for_oauth:
            raise UpstoxAuthError("OAuth is not configured")
        payload = urlencode(
            {
                "code": code,
                "client_id": self.api_key,
                "client_secret": self.api_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode("utf-8")
        request = Request(
            TOKEN_URL,
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise UpstoxAuthError(f"Upstox token exchange failed (HTTP {error.code}): {detail}")
        except (URLError, TimeoutError) as error:
            raise UpstoxAuthError(f"could not reach Upstox: {error}")
        token = body.get("access_token")
        if not token:
            raise UpstoxAuthError(f"no access_token in Upstox response: {body}")
        self.set_token(token)
        return self.status()

    def status(self) -> dict:
        if self._analytics_token:
            return {
                "has_token": True,
                "likely_valid": True,
                "token_type": "analytics",
                "updated_at": None,
                "expires_at_ist": None,
                "oauth_configured": self.configured_for_oauth,
            }
        with self._lock:
            record = self.store.get_credential(CREDENTIAL_NAME)
        has_token = bool(record and record[0])
        updated_at = record[1] if record else None
        valid = has_token
        expires_at = None
        if updated_at:
            try:
                issued = datetime.fromisoformat(updated_at)
                if issued.tzinfo is None:
                    issued = issued.replace(tzinfo=timezone.utc)
                expiry = _token_valid_until(issued)
                expires_at = expiry.isoformat()
                valid = has_token and datetime.now(IST) < expiry
            except ValueError:
                pass
        return {
            "has_token": has_token,
            "likely_valid": valid,
            "token_type": "oauth" if has_token else None,
            "updated_at": updated_at,
            "expires_at_ist": expires_at,
            "oauth_configured": self.configured_for_oauth,
        }
