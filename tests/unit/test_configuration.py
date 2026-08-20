import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration, LoadType, RowConfiguration, flatten_row_params


def test_root_config_parses_and_derives_host():
    cfg = Configuration(tenant_id="acme", realm="acme1", client_id="C", **{"#client_secret": "s"})
    assert cfg.host == "acme.ifs.cloud"
    assert cfg.advanced.page_size == 1000
    assert cfg.advanced.base_path == "/main/ifsapplications/projection/v1"


def test_advanced_defaults_when_absent():
    cfg = Configuration(tenant_id="t", realm="r", client_id="C", **{"#client_secret": "s"})
    assert cfg.advanced.page_size == 1000
    assert cfg.advanced.request_timeout == 60
    assert cfg.advanced.max_retries == 5


def test_advanced_nested_override_parses():
    cfg = Configuration(
        tenant_id="t",
        realm="r",
        client_id="C",
        advanced={"page_size": 500, "max_retries": 2},
        **{"#client_secret": "s"},
    )
    assert cfg.advanced.page_size == 500
    assert cfg.advanced.max_retries == 2
    assert cfg.advanced.request_timeout == 60  # untouched default


def test_secret_alias_matches_schema_key():
    cfg = Configuration(tenant_id="t", realm="r", client_id="C", **{"#client_secret": "sec"})
    assert cfg.client_secret == "sec"


def test_missing_required_raises_userexception():
    with pytest.raises(UserException):
        Configuration(tenant_id="t")  # missing realm/client_id/#client_secret


def test_nested_advanced_validation_error_raises_userexception():
    with pytest.raises(UserException):
        Configuration(
            tenant_id="t",
            realm="r",
            client_id="C",
            advanced={"page_size": "not-an-int"},
            **{"#client_secret": "s"},
        )


def test_row_full_load_defaults_ok():
    row = RowConfiguration(
        service="VoucherRowsAnalysis",
        entity_set="VoucherRowSet",
        load_type=LoadType.full_load,
    )
    assert row.incremental is False
    assert row.columns == []
    assert row.primary_key == []
    assert row.date_field is None


def test_row_table_name_defaults_to_entity_set():
    row = RowConfiguration(service="S", entity_set="VoucherRowSet", load_type=LoadType.full_load)
    assert row.output_table is None
    assert row.table_name == "VoucherRowSet"


def test_row_output_table_override_used_as_table_name():
    row = RowConfiguration(
        service="S",
        entity_set="VoucherRowSet",
        output_table="VoucherRowSet_decimals",
        load_type=LoadType.full_load,
    )
    assert row.table_name == "VoucherRowSet_decimals"


def test_row_output_table_blank_falls_back_to_entity_set():
    row = RowConfiguration(service="S", entity_set="VoucherRowSet", output_table="  ", load_type=LoadType.full_load)
    assert row.output_table is None
    assert row.table_name == "VoucherRowSet"


def test_row_output_table_invalid_chars_raise():
    with pytest.raises(UserException):
        RowConfiguration(
            service="S",
            entity_set="E",
            output_table="bad name.with dots",
            load_type=LoadType.full_load,
        )


def test_flatten_row_params_lifts_sections():
    nested = {
        "source": {"service": "S", "entity_set": "E", "output_table": "T"},
        "query": {"columns": ["A", "B"], "filter": "X eq 1", "order_by": "A desc"},
        "date_window": {"date_field": "D", "date_start": "2024-01-01", "date_end": "2024-02-01"},
        "load": {"load_type": "full_load", "keep_meta_fields": True},
    }
    flat = flatten_row_params(nested)
    assert flat == {
        "service": "S",
        "entity_set": "E",
        "output_table": "T",
        "columns": ["A", "B"],
        "filter": "X eq 1",
        "order_by": "A desc",
        "date_field": "D",
        "date_start": "2024-01-01",
        "date_end": "2024-02-01",
        "load_type": "full_load",
        "keep_meta_fields": True,
    }


def test_flatten_row_params_passthrough_flat():
    flat = {"service": "S", "entity_set": "E", "load_type": "full_load"}
    assert flatten_row_params(flat) == flat


def test_row_parses_from_nested_sections():
    row = RowConfiguration(
        source={"service": "VoucherRowsAnalysis", "entity_set": "VoucherRowSet"},
        query={"columns": ["Company", "Amount"]},
        date_window={"date_field": "VoucherDate", "date_start": "2026-06-11", "date_end": "2026-06-12"},
        load={"output_table": "VoucherRowSet_slice", "load_type": "incremental_load", "primary_key": ["Company"]},
    )
    assert row.service == "VoucherRowsAnalysis"
    assert row.entity_set == "VoucherRowSet"
    assert row.columns == ["Company", "Amount"]
    assert row.date_field == "VoucherDate"
    assert row.incremental is True
    assert row.primary_key == ["Company"]
    # output_table lives in the Storage section but must still resolve as the table name
    assert row.output_table == "VoucherRowSet_slice"
    assert row.table_name == "VoucherRowSet_slice"


def test_row_nested_incremental_without_pk_raises():
    with pytest.raises(UserException):
        RowConfiguration(
            source={"service": "S", "entity_set": "E"},
            load={"load_type": "incremental_load"},
        )


def test_row_incremental_load_with_pk_ok():
    row = RowConfiguration(
        service="VoucherRowsAnalysis",
        entity_set="VoucherRowSet",
        load_type=LoadType.incremental_load,
        primary_key=["Company"],
    )
    assert row.incremental is True


def test_row_incremental_load_without_pk_raises():
    with pytest.raises(UserException):
        RowConfiguration(service="S", entity_set="E", load_type=LoadType.incremental_load)


def test_row_date_fields_default_none():
    row = RowConfiguration(service="S", entity_set="E", load_type=LoadType.full_load)
    assert row.date_field is None
    assert row.date_start is None
    assert row.date_end is None


def test_row_date_fields_accept_relative_and_absolute_strings():
    row = RowConfiguration(
        service="S",
        entity_set="E",
        load_type=LoadType.full_load,
        date_field="EntryDate",
        date_start="5 days ago",
        date_end="2024-12-31",
    )
    assert row.date_field == "EntryDate"
    assert row.date_start == "5 days ago"
    assert row.date_end == "2024-12-31"


def test_row_date_start_without_date_field_raises():
    with pytest.raises(UserException):
        RowConfiguration(service="S", entity_set="E", load_type=LoadType.full_load, date_start="2024-01-01")


def test_row_date_end_without_date_field_raises():
    with pytest.raises(UserException):
        RowConfiguration(service="S", entity_set="E", load_type=LoadType.full_load, date_end="2024-12-31")


def test_extra_fields_ignored():
    cfg = Configuration(
        tenant_id="t", realm="r", client_id="C", debug=True, unknown_field="x", **{"#client_secret": "s"}
    )
    assert not hasattr(cfg, "debug")
    assert not hasattr(cfg, "unknown_field")


def test_service_account_field_removed():
    """service_account was inert UI noise; it must no longer be a config field."""
    assert "service_account" not in Configuration.model_fields
    # A legacy config still carrying it must load fine (silently ignored, not exposed).
    cfg = Configuration(tenant_id="t", realm="r", client_id="C", service_account="svc", **{"#client_secret": "s"})
    assert not hasattr(cfg, "service_account")


def test_fetch_type_field_removed():
    """fetch_type was redundant once the auto-advancing watermark was removed."""
    assert "fetch_type" not in RowConfiguration.model_fields
    # A legacy config still carrying it (and the old field names) must load fine.
    row = RowConfiguration(
        service="S",
        entity_set="E",
        load_type=LoadType.full_load,
        fetch_type="incremental_fetch",
        incremental_field="EntryDate",
    )
    assert not hasattr(row, "fetch_type")
    assert not hasattr(row, "incremental_field")
