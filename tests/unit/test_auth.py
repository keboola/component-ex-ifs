import pytest
from keboola.component.exceptions import UserException

from client.auth import IfsAuthClient


def make_handler(session):
    return IfsAuthClient("acme.ifs.cloud", "acme1", "C", "s", session=session)


def test_token_url_built_from_host_and_realm(stub_session):
    h = make_handler(stub_session)
    assert h.token_url == "https://acme.ifs.cloud/auth/realms/acme1/protocol/openid-connect/token"


def test_exchanges_and_caches_token(stub_session):
    stub_session.enqueue_token("tok-1", expires_in=180)
    h = make_handler(stub_session)
    assert h.get_token() == "tok-1"
    assert h.get_token() == "tok-1"  # cached, no 2nd POST
    assert stub_session.token_posts == 1


def test_refreshes_within_margin(stub_session):
    stub_session.enqueue_token("tok-1", expires_in=5)  # inside 30s margin
    stub_session.enqueue_token("tok-2", expires_in=180)
    h = make_handler(stub_session)
    assert h.get_token() == "tok-1"
    assert h.get_token() == "tok-2"  # re-exchanged because near expiry
    assert stub_session.token_posts == 2


def test_uses_client_secret_post_and_scope(stub_session):
    stub_session.enqueue_token("t", expires_in=180)
    make_handler(stub_session).get_token()
    body = stub_session.last_token_body
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == "C"
    assert body["client_secret"] == "s"
    assert body["scope"] == "openid microprofile-jwt"


def test_invalidate_forces_reexchange(stub_session):
    stub_session.enqueue_token("tok-1", expires_in=180)
    stub_session.enqueue_token("tok-2", expires_in=180)
    h = make_handler(stub_session)
    assert h.get_token() == "tok-1"
    h.invalidate()
    assert h.get_token() == "tok-2"
    assert stub_session.token_posts == 2


def test_auth_failure_raises_userexception(stub_session):
    stub_session.enqueue_token_error(401, "invalid_client")
    with pytest.raises(UserException):
        make_handler(stub_session).get_token()
