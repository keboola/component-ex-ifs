"""Keycloak ``client_credentials`` auth client for IFS Cloud.

A single concrete class (no ABC, no multi-grant seam): the maintainer decision
is ``client_credentials`` only. The token TTL is short (~180 s observed), so the
client caches to ``expires_in`` and refreshes mid-run under a safety margin and
whenever the OData client reports a 401 (via :meth:`invalidate`).
"""

import time

import requests
from keboola.component.exceptions import UserException

_REFRESH_MARGIN_S = 30
_DEFAULT_SCOPE = "openid microprofile-jwt"


class IfsAuthClient:
    """Exchanges and caches an OAuth2 ``client_credentials`` bearer token."""

    def __init__(
        self,
        host: str,
        realm: str,
        client_id: str,
        client_secret: str,
        scope: str = _DEFAULT_SCOPE,
        session: requests.Session | None = None,
        timeout: int = 60,
    ) -> None:
        self._host = host
        self._realm = realm
        self._cid = client_id
        self._secret = client_secret
        self._scope = scope
        self._session = session or requests.Session()
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def token_url(self) -> str:
        return f"https://{self._host}/auth/realms/{self._realm}/protocol/openid-connect/token"

    def invalidate(self) -> None:
        """Drop the cached token so the next :meth:`get_token` re-exchanges."""
        self._token = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        """Return a valid bearer token, re-exchanging when cached one is stale."""
        if self._token and time.monotonic() < self._expires_at - _REFRESH_MARGIN_S:
            return self._token
        return self._exchange()

    def _exchange(self) -> str:
        resp = self._session.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._cid,
                "client_secret": self._secret,
                "scope": self._scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise UserException(
                f"IFS token request failed ({resp.status_code}). Check tenant id, realm, client id and client secret."
            )
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + int(body.get("expires_in", 180))
        return self._token
