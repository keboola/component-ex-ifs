"""Typed Pydantic configuration models for the IFS OData extractor.

`Configuration` is the root/connection config (entered once); `RowConfiguration`
is the per-table row config (one row -> one output table). Both wrap Pydantic
``ValidationError`` into ``UserException`` so a bad config exits 1, not 2.
"""

from enum import StrEnum
from typing import Self

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field, ValidationError, computed_field, model_validator


class LoadType(StrEnum):
    """Controls the Keboola Storage write mode (full overwrite vs. upsert)."""

    full_load = "full_load"
    incremental_load = "incremental_load"


def _wrap_validation_error(exc: ValidationError) -> UserException:
    messages = [f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()]
    return UserException("Validation Error: " + ", ".join(messages))


class AdvancedConfig(BaseModel):
    """Optional tuning fields rendered as a collapsible Advanced section."""

    model_config = {"extra": "ignore", "populate_by_name": True}

    base_path: str = "/main/ifsapplications/projection/v1"
    page_size: int = 1000
    request_timeout: int = 60
    max_retries: int = 5


class Configuration(BaseModel):
    """Root connection config for one IFS Cloud tenant."""

    model_config = {"extra": "ignore", "populate_by_name": True}

    tenant_id: str
    realm: str
    client_id: str
    client_secret: str = Field(alias="#client_secret")
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _wrap_validation_error(exc) from exc

    @computed_field
    @property
    def host(self) -> str:
        return f"{self.tenant_id}.ifs.cloud"


class RowConfiguration(BaseModel):
    """Per-table row config: one projection service + entity set -> one table.

    Fetching is stateless: the server-side date window (``date_field`` +
    ``date_start`` / ``date_end``) is recomputed each run from the customer's
    config. There is no stored watermark and no auto-advancing cursor — an empty
    date window fetches everything.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    service: str
    entity_set: str
    columns: list[str] = []
    primary_key: list[str] = []
    date_field: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    load_type: LoadType = LoadType.incremental_load
    filter: str | None = None
    order_by: str | None = None
    keep_meta_fields: bool = False

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _wrap_validation_error(exc) from exc

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.incremental_load

    @model_validator(mode="after")
    def _validate_requirements(self) -> Self:
        if self.load_type == LoadType.incremental_load and not self.primary_key:
            raise ValueError("primary_key is required when load_type is incremental_load")
        if (self.date_start or self.date_end) and not self.date_field:
            raise ValueError("date_field is required when date_start or date_end is set")
        return self
