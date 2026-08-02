import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration, FetchType, LoadType, RowConfiguration


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


def test_row_full_fetch_full_load_defaults_ok():
    row = RowConfiguration(
        service="VoucherRowsAnalysis",
        entity_set="VoucherRowSet",
        load_type=LoadType.full_load,
        fetch_type=FetchType.full_fetch,
    )
    assert row.incremental is False
    assert row.columns == []
    assert row.primary_key == []


def test_row_incremental_requires_fields_when_incremental():
    row = RowConfiguration(
        service="VoucherRowsAnalysis",
        entity_set="VoucherRowSet",
        load_type=LoadType.incremental_load,
        primary_key=["Company"],
        fetch_type=FetchType.incremental_fetch,
        incremental_field="EntryDate",
    )
    assert row.incremental is True


def test_row_incremental_load_without_pk_raises():
    with pytest.raises(UserException):
        RowConfiguration(service="S", entity_set="E", load_type=LoadType.incremental_load)


def test_row_incremental_fetch_without_field_raises():
    with pytest.raises(UserException):
        RowConfiguration(
            service="S",
            entity_set="E",
            load_type=LoadType.full_load,
            fetch_type=FetchType.incremental_fetch,
        )


def test_extra_fields_ignored():
    cfg = Configuration(
        tenant_id="t", realm="r", client_id="C", debug=True, unknown_field="x", **{"#client_secret": "s"}
    )
    assert not hasattr(cfg, "debug")
    assert not hasattr(cfg, "unknown_field")
