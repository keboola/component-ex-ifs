from pathlib import Path

from client.metadata import edm_to_base_type, parse_metadata, rank_incremental_fields

FIXTURE = Path(__file__).parent / "fixtures" / "voucher_metadata.xml"


def _load() -> dict:
    return parse_metadata(FIXTURE.read_text(encoding="utf-8"))


def test_edm_to_base_type_covers_all():
    assert edm_to_base_type("Edm.String") == "STRING"
    assert edm_to_base_type("Edm.Guid") == "STRING"
    assert edm_to_base_type("Edm.Binary") == "STRING"
    assert edm_to_base_type("Edm.Boolean") == "BOOLEAN"
    assert edm_to_base_type("Edm.Int16") == "INTEGER"
    assert edm_to_base_type("Edm.Int32") == "INTEGER"
    assert edm_to_base_type("Edm.Int64") == "INTEGER"
    assert edm_to_base_type("Edm.Byte") == "INTEGER"
    assert edm_to_base_type("Edm.Decimal") == "NUMERIC"
    assert edm_to_base_type("Edm.Double") == "FLOAT"
    assert edm_to_base_type("Edm.Single") == "FLOAT"
    assert edm_to_base_type("Edm.Date") == "DATE"
    assert edm_to_base_type("Edm.DateTimeOffset") == "TIMESTAMP"
    assert edm_to_base_type("Edm.DateTime") == "TIMESTAMP"
    assert edm_to_base_type("Edm.TimeOfDay") == "STRING"
    assert edm_to_base_type("Edm.Something") == "STRING"  # fallback


def test_parse_metadata_entity_sets_and_types():
    meta = _load()
    assert set(meta) == {"VoucherRowSet", "AccountSet"}
    vr = meta["VoucherRowSet"]
    assert vr.entity_type == "VoucherRow"
    prop_names = [p.name for p in vr.properties]
    assert prop_names == [
        "Company",
        "VoucherNo",
        "RowNo",
        "Amount",
        "Rate",
        "EntryDate",
        "ChangedTimestamp",
        "Approved",
    ]


def test_parse_metadata_keys_and_navprops():
    vr = _load()["VoucherRowSet"]
    assert vr.keys == ["Company", "VoucherNo"]
    assert vr.nav_properties == ["AccountRef"]


def test_property_precision_scale_and_nullable():
    props = {p.name: p for p in _load()["VoucherRowSet"].properties}
    assert props["Company"].nullable is False
    assert props["Amount"].edm_type == "Edm.Decimal"
    assert props["Amount"].precision == 20
    assert props["Amount"].scale == 2
    assert props["RowNo"].nullable is True


def test_rank_incremental_fields_datetime_first():
    props = _load()["VoucherRowSet"].properties
    ranked = rank_incremental_fields(props)
    # Timestamp then date float to the top
    assert ranked[0] == "ChangedTimestamp"
    assert ranked[1] == "EntryDate"
    # non-datetime columns still present, after the datetime ones
    assert set(ranked) == {p.name for p in props}
    assert ranked.index("ChangedTimestamp") < ranked.index("Company")
