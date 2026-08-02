import csv
import json
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


def _make_datadir(tmp_path: Path, params: dict, state: dict | None = None) -> Path:
    datadir = tmp_path / "data"
    (datadir / "in" / "tables").mkdir(parents=True)
    (datadir / "out" / "tables").mkdir(parents=True)
    config = {
        "parameters": params,
        "storage": {"output": {"data_type_support": "authoritative"}},
        "action": "run",
    }
    (datadir / "config.json").write_text(json.dumps(config))
    if state is not None:
        (datadir / "in" / "state.json").write_text(json.dumps(state))
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
        return list(reader.fieldnames), list(reader)


def test_full_fetch_writes_rows_and_schema_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    datadir = _make_datadir(tmp_path, {**BASE_PARAMS, "fetch_type": "full_fetch", "load_type": "full_load"})
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


def test_incremental_builds_gt_filter_and_advances_state(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "fetch_type": "incremental_fetch",
        "incremental_field": "EntryDate",
        "load_type": "incremental_load",
    }
    datadir = _make_datadir(tmp_path, params, state={"last_value": "2025-01-01"})
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("filter") == "EntryDate gt 2025-01-01"  # gt, prior bound, unquoted date
    state = json.loads((datadir / "out" / "state.json").read_text())
    assert state["last_value"] == "2025-03-31"  # advanced to new max EntryDate
    assert state["records_extracted"] == 2
    assert "last_run" in state

    manifest = json.loads((datadir / "out" / "tables" / "VoucherRowSet.csv.manifest").read_text())
    assert manifest.get("incremental") is True


def test_empty_state_incremental_has_no_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    params = {
        **BASE_PARAMS,
        "fetch_type": "incremental_fetch",
        "incremental_field": "EntryDate",
        "load_type": "incremental_load",
    }
    datadir = _make_datadir(tmp_path, params)  # no prior state
    monkeypatch.setenv("KBC_DATADIR", str(datadir))
    captured: dict = {}
    _patch_client(monkeypatch, captured=captured)

    from component import Component

    Component().run()

    assert captured.get("filter") is None  # first run -> full fetch, no watermark clause


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
