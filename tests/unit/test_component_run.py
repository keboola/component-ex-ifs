import csv
import json
import logging
from pathlib import Path

import pytest
from keboola.component.exceptions import UserException

from client import odata

FIXTURE = Path(__file__).parent / "fixtures" / "voucher_metadata.xml"

BASE_PARAMS = {
    "tenant_id": "acme",
    "realm": "acme1",
    "client_id": "C",
    "#client_secret": "s",
    "service": "VoucherRowsAnalysis",
    "entity_set": "VoucherRowSet",
    "primary_key": ["Company", "VoucherNo"],
}

ROWS = [
    {
        "Company": "100",
        "VoucherNo": "V1",
        "RowNo": 1,
        "Amount": 10.5,
        "Rate": 1.0,
        "EntryDate": "2025-01-15",
        "ChangedTimestamp": "2025-01-15T00:00:00Z",
        "Approved": True,
        "@odata.etag": "etag-1",
        "luname": "VoucherRow",
    },
    {
        "Company": "100",
        "VoucherNo": "V2",
        "RowNo": 2,
        "Amount": 20.0,
        "Rate": 1.0,
        "EntryDate": "2025-03-31",
        "ChangedTimestamp": "2025-03-31T00:00:00Z",
        "Approved": False,
    },
]


def _make_datadir(tmp_path: Path, params: dict) -> Path:
    datadir = tmp_path / "data"
    (datadir / "in" / "tables").mkdir(parents=True)
    (datadir / "out" / "tables").mkdir(parents=True)
    config = {
        "parameters": params,
        "storage": {"output": {"data_type_support": "authoritative"}},
        "action": "run",
    }
    (datadir / "config.json").write_text(json.dumps(config))
    return datadir


def _patch_client(monkeypatch, rows=ROWS, captured=None):
    monkeypatch.setattr(odata.IfsODataClient, "get_metadata", lambda self, service: FIXTURE.read_text(encoding="utf-8"))

    def fake_iter_rows(self, service, entity_set, **kwargs):
        if captured is not None:
            captured.update(kwargs)
        yield from rows

    monkeypatch.setattr(odata.IfsODataClient, "iter_rows", fake_iter_rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        return fieldnames, list(reader)


def test_full_extract_writes_rows_and_schema_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    datadir = _make_datadir(tmp_path, {**BASE_PARAMS, "load_type": "full_load"})
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    _patch_client(monkeypatch)

    from component import Component

    Component().run()

    out = datadir / "out" / "tables" / "VoucherRowSet.csv"
    header, records = _read_csv(out)
    assert len(records) == 2
    # meta fields stripped: not present in the authoritative schema columns
    assert "@odata.etag" not in header
    assert "luname" not in header
    assert "Company" in header and "EntryDate" in header

    manifest = json.loads((out.parent / "VoucherRowSet.csv.manifest").read_text())
    assert manifest.get("schema"), "authoritative schema manifest expected"
    pk_cols = [c["name"] for c in manifest["schema"] if c.get("primary_key")]
    assert set(pk_cols) == {"Company", "VoucherNo"}


def test_no_date_window_and_no_filter_sends_no_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    datadir = _make_datadir(tmp_path, {**BASE_PARAMS, "load_type": "full_load"})
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("filter") is None  # empty window + no user filter -> fetch everything


def test_date_start_builds_ge_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_start": "2024-01-01",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("filter") == "EntryDate ge 2024-01-01"  # ge lower bound, unquoted Edm.Date


def test_date_end_builds_lt_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_end": "2025-06-30",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("filter") == "EntryDate lt 2025-06-30"  # lt upper bound


def test_date_window_both_bounds_combined_with_user_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_start": "2020-01-01",
        "date_end": "2025-06-30",
        "filter": "Company eq '100'",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    # user filter AND date_start (ge) AND date_end (lt), each clause parenthesised
    assert captured.get("filter") == "(Company eq '100') and (EntryDate ge 2020-01-01) and (EntryDate lt 2025-06-30)"


def test_date_start_relative_value_resolves_to_concrete_date(tmp_path, monkeypatch):
    from freezegun import freeze_time

    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_start": "5 days ago",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    with freeze_time("2026-08-03"):
        Component().run()

    assert captured.get("filter") == "EntryDate ge 2026-07-29"  # 2026-08-03 minus 5 days


def test_date_end_timestamp_field_renders_iso8601_literal(tmp_path, monkeypatch):
    """A timestamp Date Field yields an unquoted ISO-8601 …Z literal."""
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "ChangedTimestamp",
        "date_end": "2025-06-30",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("filter") == "ChangedTimestamp lt 2025-06-30T00:00:00Z"


def test_columns_sent_verbatim_without_forcing_date_field(tmp_path, monkeypatch):
    """The Date Field is not force-added to $select — $filter may reference an unselected column."""
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_start": "2024-01-01",
        "columns": ["Company", "VoucherNo", "Amount"],  # EntryDate deliberately omitted
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("select") == ["Company", "VoucherNo", "Amount"]  # verbatim, no EntryDate appended


def test_unparseable_date_start_raises_userexception(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_start": "definitely not a date",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    _patch_client(monkeypatch)

    from component import Component

    with pytest.raises(UserException):
        Component().run()


def test_state_file_carries_no_data_cursor(tmp_path, monkeypatch):
    """State is operational only (last_run + records_extracted) — no watermark/last_value cursor."""
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "load_type": "incremental_load",
        "date_field": "EntryDate",
        "date_start": "2024-01-01",
    }
    datadir = _make_datadir(tmp_path, params)
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    _patch_client(monkeypatch)

    from component import Component

    Component().run()

    state = json.loads((datadir / "out" / "state.json").read_text())
    assert state["records_extracted"] == 2
    assert "last_run" in state
    assert "last_value" not in state  # no auto-advancing cursor persisted


def test_keep_meta_fields_true_writes_meta_columns(tmp_path, monkeypatch):
    """keep_meta_fields=true surfaces the IFS meta-fields as output columns with values."""
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    datadir = _make_datadir(tmp_path, {**BASE_PARAMS, "load_type": "full_load", "keep_meta_fields": True})
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    _patch_client(monkeypatch)

    from component import Component

    Component().run()

    out = datadir / "out" / "tables" / "VoucherRowSet.csv"
    header, records = _read_csv(out)
    for name in ("@odata.etag", "luname", "keyref", "Objgrants"):
        assert name in header  # meta-fields now materialise as columns
    assert records[0]["@odata.etag"] == "etag-1"  # value from the source row is retained
    assert records[0]["luname"] == "VoucherRow"

    manifest = json.loads((out.parent / "VoucherRowSet.csv.manifest").read_text())
    meta_cols = {c["name"] for c in manifest["schema"] if c["name"].startswith("@odata.") or c["name"] == "luname"}
    assert {"@odata.etag", "luname"} <= meta_cols


def test_forbidden_from_client_raises_userexception(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    datadir = _make_datadir(tmp_path, {**BASE_PARAMS, "load_type": "full_load"})
    monkeypatch.setenv("KBC_DATADIR", str(datadir))

    monkeypatch.setattr(odata.IfsODataClient, "get_metadata", lambda self, service: FIXTURE.read_text(encoding="utf-8"))

    def raising_iter(self, service, entity_set, **kwargs):
        raise UserException("IFS request failed (403): No allowed company")
        yield  # pragma: no cover

    monkeypatch.setattr(odata.IfsODataClient, "iter_rows", raising_iter)

    from component import Component

    with pytest.raises(UserException):
        Component().run()


def test_zero_rows_logs_allowed_company_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    datadir = _make_datadir(tmp_path, {**BASE_PARAMS, "load_type": "full_load"})
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    _patch_client(monkeypatch, rows=[])  # successful run, zero rows

    from component import Component

    with caplog.at_level(logging.WARNING):
        Component().run()

    assert any("Allowed Companies" in record.getMessage() for record in caplog.records)
