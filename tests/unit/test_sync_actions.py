import json
from pathlib import Path

import pytest
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType

from client import auth as auth_module
from client import odata

FIXTURE = Path(__file__).parent / "fixtures" / "voucher_metadata.xml"

CONN = {
    "tenant_id": "acme",
    "realm": "acme1",
    "client_id": "C",
    "#client_secret": "s",
}


def _component(tmp_path, monkeypatch, params):
    datadir = tmp_path / "data"
    (datadir / "in" / "tables").mkdir(parents=True)
    (datadir / "out" / "tables").mkdir(parents=True)
    # action "run" -> the sync-action wrapper runs the method inline and lets it
    # return / raise normally (no stdout capture, no exit()), ideal for unit tests.
    config = {"parameters": params, "action": "run"}
    (datadir / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    from component import Component

    return Component()


def _values(items):
    return [i.value for i in items]


def test_test_connection_success(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module.IfsAuthClient, "get_token", lambda self: "tok")
    comp = _component(tmp_path, monkeypatch, CONN)
    result = comp.test_connection()
    assert result.type == MessageType.SUCCESS


def test_test_connection_failure_raises(tmp_path, monkeypatch):
    def boom(self):
        raise UserException("IFS token request failed (401).")

    monkeypatch.setattr(auth_module.IfsAuthClient, "get_token", boom)
    comp = _component(tmp_path, monkeypatch, CONN)
    with pytest.raises(UserException):
        comp.test_connection()


def test_test_connection_missing_config_raises(tmp_path, monkeypatch):
    comp = _component(tmp_path, monkeypatch, {"tenant_id": "acme"})  # incomplete
    with pytest.raises(UserException):
        comp.test_connection()


def test_list_services_returns_dropdown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        odata.IfsODataClient,
        "list_projections",
        lambda self: [{"Name": "VoucherRowsAnalysis"}, {"Name": "OtherService"}],
    )
    comp = _component(tmp_path, monkeypatch, CONN)
    items = comp.list_services()
    assert _values(items) == ["VoucherRowsAnalysis", "OtherService"]


def test_list_services_catalog_unavailable_guidance(tmp_path, monkeypatch):
    def unavailable(self):
        raise UserException("catalog unavailable")

    monkeypatch.setattr(odata.IfsODataClient, "list_projections", unavailable)
    comp = _component(tmp_path, monkeypatch, CONN)
    items = comp.list_services()
    assert len(items) == 1
    assert items[0].value == ""  # free-text fallback guidance item, not a crash


def test_list_services_missing_connection_guidance(tmp_path, monkeypatch):
    comp = _component(tmp_path, monkeypatch, {})  # no connection
    items = comp.list_services()
    assert items[0].value == ""


def test_list_entitysets_missing_service_guidance(tmp_path, monkeypatch):
    comp = _component(tmp_path, monkeypatch, CONN)  # no service
    items = comp.list_entitysets()
    assert items[0].value == ""


def test_list_entitysets_from_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(odata.IfsODataClient, "get_metadata", lambda self, service: FIXTURE.read_text(encoding="utf-8"))
    comp = _component(tmp_path, monkeypatch, {**CONN, "service": "VoucherRowsAnalysis"})
    items = comp.list_entitysets()
    assert set(_values(items)) == {"VoucherRowSet", "AccountSet"}


def test_list_columns_scalar_properties_only(tmp_path, monkeypatch):
    monkeypatch.setattr(odata.IfsODataClient, "get_metadata", lambda self, service: FIXTURE.read_text(encoding="utf-8"))
    comp = _component(tmp_path, monkeypatch, {**CONN, "service": "VoucherRowsAnalysis", "entity_set": "VoucherRowSet"})
    values = _values(comp.list_columns())
    assert "Company" in values
    assert "EntryDate" in values
    # navigation properties are excluded — OData $select rejects them with a 400
    assert "AccountRef" not in values


def test_list_primary_keys_keys_first(tmp_path, monkeypatch):
    monkeypatch.setattr(odata.IfsODataClient, "get_metadata", lambda self, service: FIXTURE.read_text(encoding="utf-8"))
    comp = _component(tmp_path, monkeypatch, {**CONN, "service": "VoucherRowsAnalysis", "entity_set": "VoucherRowSet"})
    values = _values(comp.list_primary_keys())
    assert values[:2] == ["Company", "VoucherNo"]  # EDMX key columns ranked first


def test_list_date_fields_datetime_first(tmp_path, monkeypatch):
    monkeypatch.setattr(odata.IfsODataClient, "get_metadata", lambda self, service: FIXTURE.read_text(encoding="utf-8"))
    comp = _component(tmp_path, monkeypatch, {**CONN, "service": "VoucherRowsAnalysis", "entity_set": "VoucherRowSet"})
    values = _values(comp.list_date_fields())
    assert values[0] == "ChangedTimestamp"
    assert values[1] == "EntryDate"


def test_validate_query_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(odata.IfsODataClient, "validate_query", lambda self, *a, **k: (True, "Query is valid."))
    comp = _component(tmp_path, monkeypatch, {**CONN, "service": "VoucherRowsAnalysis", "entity_set": "VoucherRowSet"})
    result = comp.validate_query()
    assert result.type == MessageType.SUCCESS


def test_validate_query_surfaces_odata_error(tmp_path, monkeypatch):
    monkeypatch.setattr(odata.IfsODataClient, "validate_query", lambda self, *a, **k: (False, "Invalid filter clause"))
    comp = _component(tmp_path, monkeypatch, {**CONN, "service": "VoucherRowsAnalysis", "entity_set": "VoucherRowSet"})
    result = comp.validate_query()
    assert result.type in (MessageType.ERROR, MessageType.WARNING)
    assert "Invalid filter clause" in result.message


def test_validate_query_missing_selection_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module.IfsAuthClient, "get_token", lambda self: "tok")
    comp = _component(tmp_path, monkeypatch, CONN)  # no service/entity_set
    result = comp.validate_query()
    assert result.type == MessageType.WARNING
