import pytest
from keboola.component.exceptions import UserException

from client import odata
from client.odata import IfsODataClient

HOST = "acme.ifs.cloud"
BASE = "/main/ifsapplications/projection/v1"


def make_client(session, auth, page_size=1000):
    return IfsODataClient(
        host=HOST,
        base_path=BASE,
        auth=auth,
        session=session,
        page_size=page_size,
        timeout=60,
        max_retries=3,
    )


def test_service_url_built():
    client = make_client(None, None)
    assert client.service_url("VoucherRowsAnalysis") == (f"https://{HOST}{BASE}/VoucherRowsAnalysis.svc")


def test_iter_rows_follows_nextlink_and_strips_meta(stub_session, auth_stub):
    stub_session.enqueue_json(
        {
            "value": [{"A": 1, "@odata.etag": "x", "luname": "L", "keyref": "k", "Objgrants": "g"}],
            "@odata.nextLink": "VoucherRowsAnalysis.svc/VoucherRowSet?$skiptoken=1",
        }
    )
    stub_session.enqueue_json({"value": [{"A": 2}]})
    client = make_client(stub_session, auth_stub)
    rows = list(client.iter_rows("VoucherRowsAnalysis", "VoucherRowSet"))
    assert rows == [{"A": 1}, {"A": 2}]  # meta stripped, both pages


def test_iter_rows_keeps_meta_when_disabled(stub_session, auth_stub):
    stub_session.enqueue_json({"value": [{"A": 1, "luname": "L"}]})
    client = make_client(stub_session, auth_stub)
    rows = list(client.iter_rows("Svc", "Set", strip_meta=False))
    assert rows == [{"A": 1, "luname": "L"}]


def test_iter_rows_skip_fallback_on_full_page(stub_session, auth_stub):
    # page_size 2, first page returns exactly 2 rows and NO nextLink -> $skip fallback
    stub_session.enqueue_json({"value": [{"A": 1}, {"A": 2}]})
    stub_session.enqueue_json({"value": [{"A": 3}]})  # short page -> stop
    client = make_client(stub_session, auth_stub, page_size=2)
    rows = list(client.iter_rows("Svc", "Set"))
    assert rows == [{"A": 1}, {"A": 2}, {"A": 3}]
    assert "$skip=2" in stub_session.get_urls[1]


def test_iter_rows_applies_query_options(stub_session, auth_stub):
    stub_session.enqueue_json({"value": []})
    client = make_client(stub_session, auth_stub)
    list(client.iter_rows("Svc", "Set", select=["A", "B"], filter="A gt 1", orderby="A"))
    url = stub_session.get_urls[0]
    assert "$select=A,B" in url
    assert "$orderby=A" in url
    assert "%24filter" in url or "$filter=A" in url


def test_forbidden_raises_userexception_with_allowed_company_hint(stub_session, auth_stub):
    stub_session.enqueue_json({"error": {"code": "forbidden", "message": "No allowed company"}}, status_code=403)
    client = make_client(stub_session, auth_stub)
    with pytest.raises(UserException) as exc:
        list(client.iter_rows("Svc", "Set"))
    message = str(exc.value)
    assert "403" in message
    assert "Allowed Companies" in message  # actionable diagnostic
    assert "Permission Set" in message


def test_validate_query_403_returns_actionable_hint(stub_session, auth_stub):
    stub_session.enqueue_json({"error": {"message": "denied"}}, status_code=403)
    client = make_client(stub_session, auth_stub)
    ok, message = client.validate_query("Svc", "Set")
    assert ok is False
    assert "Allowed Companies" in message


def test_bad_filter_400_surfaces_odata_message(stub_session, auth_stub):
    stub_session.enqueue_json({"error": {"message": {"lang": "en", "value": "Invalid filter"}}}, status_code=400)
    client = make_client(stub_session, auth_stub)
    with pytest.raises(UserException) as exc:
        list(client.iter_rows("Svc", "Set"))
    assert "Invalid filter" in str(exc.value)


def test_401_invalidates_and_retries_once(stub_session, auth_stub):
    stub_session.enqueue_json({}, status_code=401)
    stub_session.enqueue_json({"value": [{"A": 1}]})
    client = make_client(stub_session, auth_stub)
    rows = list(client.iter_rows("Svc", "Set"))
    assert rows == [{"A": 1}]
    assert auth_stub.invalidations == 1


def test_429_retries_with_backoff(stub_session, auth_stub, monkeypatch):
    monkeypatch.setattr(odata.time, "sleep", lambda _s: None)
    stub_session.enqueue_json({}, status_code=429, headers={"Retry-After": "1"})
    stub_session.enqueue_json({"value": [{"A": 1}]})
    client = make_client(stub_session, auth_stub)
    rows = list(client.iter_rows("Svc", "Set"))
    assert rows == [{"A": 1}]


def test_get_metadata_returns_raw_text(stub_session, auth_stub):
    stub_session.enqueue_json({"edmx": "stub"})
    client = make_client(stub_session, auth_stub)
    text = client.get_metadata("Svc")
    assert "edmx" in text
    assert stub_session.get_urls[0].endswith("/Svc.svc/$metadata")


def test_list_projections_pages_and_returns_dicts(stub_session, auth_stub):
    stub_session.enqueue_json(
        {
            "value": [{"Name": "VoucherRowsAnalysis"}],
            "@odata.nextLink": "AllProjections.svc/Projections?$skiptoken=1",
        }
    )
    stub_session.enqueue_json({"value": [{"Name": "OtherService"}]})
    client = make_client(stub_session, auth_stub)
    projections = client.list_projections()
    assert projections == [{"Name": "VoucherRowsAnalysis"}, {"Name": "OtherService"}]


def test_validate_query_ok_on_200(stub_session, auth_stub):
    stub_session.enqueue_json({"value": [{"A": 1}]})
    client = make_client(stub_session, auth_stub)
    ok, message = client.validate_query("Svc", "Set", select=["A"], filter="A gt 1")
    assert ok is True
    assert "valid" in message.lower()
    assert "$top=1" in stub_session.get_urls[0]


def test_validate_query_returns_odata_error_on_400(stub_session, auth_stub):
    stub_session.enqueue_json({"error": {"message": {"lang": "en", "value": "Invalid filter clause"}}}, status_code=400)
    client = make_client(stub_session, auth_stub)
    ok, message = client.validate_query("Svc", "Set", filter="bad filter")
    assert ok is False
    assert "Invalid filter clause" in message


def test_validate_query_raises_on_transport_error(stub_session, auth_stub):
    stub_session.enqueue_json({}, status_code=503)
    client = make_client(stub_session, auth_stub)
    with pytest.raises(UserException):
        client.validate_query("Svc", "Set")
