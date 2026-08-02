from keboola.component.dao import SupportedDataTypes

from client.metadata import EntityMeta, PropertyMeta
from client.schema import build_column_schema, format_filter_literal


def _entity() -> EntityMeta:
    return EntityMeta(
        entity_set="VoucherRowSet",
        entity_type="VoucherRow",
        properties=[
            PropertyMeta("Company", "Edm.String", nullable=False),
            PropertyMeta("VoucherNo", "Edm.String", nullable=False),
            PropertyMeta("Amount", "Edm.Decimal", nullable=True, precision=20, scale=2),
            PropertyMeta("RowNo", "Edm.Int64", nullable=True),
            PropertyMeta("EntryDate", "Edm.Date", nullable=True),
        ],
        keys=["Company", "VoucherNo"],
    )


def test_build_column_schema_all_columns_base_types():
    schema = build_column_schema(_entity(), None)
    assert list(schema.keys()) == ["Company", "VoucherNo", "Amount", "RowNo", "EntryDate"]
    assert schema["Company"].data_types["base"].dtype == SupportedDataTypes.STRING
    assert schema["Amount"].data_types["base"].dtype == SupportedDataTypes.NUMERIC
    assert schema["RowNo"].data_types["base"].dtype == SupportedDataTypes.INTEGER
    assert schema["EntryDate"].data_types["base"].dtype == SupportedDataTypes.DATE


def test_build_column_schema_respects_selection():
    schema = build_column_schema(_entity(), ["Company", "EntryDate"])
    assert list(schema.keys()) == ["Company", "EntryDate"]


def test_build_column_schema_marks_primary_key_and_nullable():
    schema = build_column_schema(_entity(), None, primary_key=["Company", "VoucherNo"])
    assert schema["Company"].primary_key is True
    assert schema["Company"].nullable is False
    assert schema["Amount"].primary_key is False
    assert schema["Amount"].nullable is True


def test_build_column_schema_decimal_precision_scale_length():
    schema = build_column_schema(_entity(), None)
    assert schema["Amount"].data_types["base"].length == "20,2"


def test_format_filter_literal_types():
    assert format_filter_literal("Edm.Date", "2025-01-01") == "2025-01-01"
    assert format_filter_literal("Edm.DateTimeOffset", "2025-01-01T00:00:00Z") == "2025-01-01T00:00:00Z"
    assert format_filter_literal("Edm.Int64", 42) == "42"
    assert format_filter_literal("Edm.Decimal", "10.5") == "10.5"
    assert format_filter_literal("Edm.String", "100") == "'100'"


def test_format_filter_literal_escapes_quotes():
    assert format_filter_literal("Edm.String", "O'Brien") == "'O''Brien'"
