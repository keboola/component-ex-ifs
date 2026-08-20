"""Shared test fixtures: a fake ``requests.Session`` recording calls and
serving queued responses, used by the auth and OData client unit tests.
"""

import json as _json
from collections import deque
from collections.abc import Mapping

import pytest


class _StubResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: Mapping | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = dict(headers or {})
        self.text = _json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class StubSession:
    """Minimal stand-in for ``requests.Session`` for unit tests.

    Token responses are queued via :meth:`enqueue_token` /
    :meth:`enqueue_token_error`; arbitrary GET responses via
    :meth:`enqueue_json` / :meth:`enqueue_response`. Records the number of
    token POSTs, the last token body, and every GET URL requested.
    """

    def __init__(self):
        self._token_queue: deque[_StubResponse] = deque()
        self._get_queue: deque[_StubResponse] = deque()
        self.token_posts = 0
        self.last_token_body: dict | None = None
        self.get_urls: list[str] = []

    # --- queue helpers -------------------------------------------------
    def enqueue_token(self, access_token: str, expires_in: int = 180):
        self._token_queue.append(_StubResponse(200, {"access_token": access_token, "expires_in": expires_in}))

    def enqueue_token_error(self, status_code: int, error: str = "invalid_client"):
        self._token_queue.append(_StubResponse(status_code, {"error": error}))

    def enqueue_json(self, payload: dict, status_code: int = 200, headers: Mapping | None = None):
        self._get_queue.append(_StubResponse(status_code, payload, headers))

    def enqueue_response(self, response: _StubResponse):
        self._get_queue.append(response)

    # --- requests.Session surface --------------------------------------
    def post(self, url, data=None, headers=None, timeout=None):
        self.token_posts += 1
        self.last_token_body = dict(data or {})
        if not self._token_queue:
            raise AssertionError("No token response queued")
        return self._token_queue.popleft()

    def get(self, url, headers=None, timeout=None):
        self.get_urls.append(url)
        if not self._get_queue:
            raise AssertionError(f"No GET response queued for {url}")
        return self._get_queue.popleft()


class StubAuth:
    """Stand-in for ``IfsAuthClient`` for OData client tests."""

    def __init__(self, token: str = "tok-1"):
        self._token = token
        self.invalidations = 0

    def get_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        self.invalidations += 1
        self._token = f"{self._token}-refreshed"


@pytest.fixture
def stub_session() -> StubSession:
    return StubSession()


@pytest.fixture
def auth_stub() -> StubAuth:
    return StubAuth()
