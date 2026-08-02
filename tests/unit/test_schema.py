from keboola.component.dao import ColumnDefinition, DataType, SupportedDataTypes

from client.metadata import EntityMeta, PropertyMeta
from client.schema import build_column_schema, format_filter_literal


def _base(column: ColumnDefinition) -> DataType:
    """Return a column's base DataType, narrowing the library's Optional field."""
    data_types = column.data_types
    assert data_types is not None
    return data_types["base"]


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
    assert _base(schema["Company"]).dtype == SupportedDataTypes.STRING
    assert _base(schema["Amount"]).dtype == SupportedDataTypes.NUMERIC
    assert _base(schema["RowNo"]).dtype == SupportedDataTypes.INTEGER
    assert _base(schema["EntryDate"]).dtype == SupportedDataTypes.DATE


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
    assert _base(schema["Amount"]).length == "20,2"


def test_decimal_length_clamped_to_snowflake_max():
    """IFS nominal (130, 65) exceeds Snowflake NUMBER(38, <=37); it must clamp."""
    entity = EntityMeta(
        entity_set="X",
        entity_type="X",
        properties=[PropertyMeta("Amount", "Edm.Decimal", nullable=True, precision=130, scale=65)],
        keys=[],
    )
    schema = build_column_schema(entity, None)
    assert _base(schema["Amount"]).length == "38,10"


def test_decimal_length_valid_precision_scale_passes_through():
    """An already-valid (20, 4) is emitted verbatim (no clamping)."""
    entity = EntityMeta(
        entity_set="X",
        entity_type="X",
        properties=[PropertyMeta("Amount", "Edm.Decimal", nullable=True, precision=20, scale=4)],
        keys=[],
    )
    schema = build_column_schema(entity, None)
    assert _base(schema["Amount"]).length == "20,4"


def test_keep_meta_fields_appends_trailing_string_columns():
    schema = build_column_schema(_entity(), None, keep_meta_fields=True)
    # Real data columns first, in EDMX order, then the meta-fields as trailing STRING columns.
    assert list(schema.keys()) == [
        "Company",
        "VoucherNo",
        "Amount",
        "RowNo",
        "EntryDate",
        "@odata.etag",
        "luname",
        "keyref",
        "Objgrants",
    ]
    assert _base(schema["@odata.etag"]).dtype == SupportedDataTypes.STRING
    assert schema["Objgrants"].nullable is True


def test_meta_fields_absent_by_default():
    schema = build_column_schema(_entity(), None)
    for name in ("@odata.etag", "luname", "keyref", "Objgrants"):
        assert name not in schema


def test_format_filter_literal_types():
    assert format_filter_literal("Edm.Date", "2025-01-01") == "2025-01-01"
    assert format_filter_literal("Edm.DateTimeOffset", "2025-01-01T00:00:00Z") == "2025-01-01T00:00:00Z"
    assert format_filter_literal("Edm.Int64", 42) == "42"
    assert format_filter_literal("Edm.Decimal", "10.5") == "10.5"
    assert format_filter_literal("Edm.String", "100") == "'100'"


def test_format_filter_literal_escapes_quotes():
    assert format_filter_literal("Edm.String", "O'Brien") == "'O''Brien'"
