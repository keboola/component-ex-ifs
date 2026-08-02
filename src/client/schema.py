"""Authoritative output schema builder and type-aware ``$filter`` literals.

Pure functions (no I/O) so they unit-test without the platform. The schema is
the authoritative ``data_type.base.type`` form; ``format_filter_literal`` renders
a value into an OData ``$filter`` literal respecting its EDM type (used to build
the incremental ``gt`` watermark clause).
"""

from collections import OrderedDict

from keboola.component.dao import BaseType, ColumnDefinition, SupportedDataTypes

from client.metadata import META_FIELDS, EntityMeta, PropertyMeta, edm_to_base_type

# IFS EDMX declares every ``Edm.Decimal`` with its nominal maximum
# ``Precision=130 Scale=65``. Keboola Storage on Snowflake materialises a
# NUMERIC column as ``NUMBER(precision, scale)``, but Snowflake caps precision
# at 38 and requires ``scale <= precision`` (absolute max scale 37); a raw
# ``NUMBER(130,65)`` is rejected and the column fails to import under
# ``dataTypeSupport=authoritative`` — making the finance projections unloadable.
# So we clamp any out-of-range ``(precision, scale)`` into a Snowflake-valid
# range while preserving generous integer room: precision at most 38 and
# fractional digits at most 10 (ample for IFS financial amounts), which still
# leaves >= 28 integer digits. IFS ``(130,65)`` -> ``NUMBER(38,10)``; an
# already-valid pair such as ``(20,4)`` passes through unchanged.
_SF_MAX_PRECISION = 38
_SF_MAX_SCALE = 37  # Snowflake's absolute scale ceiling
_TARGET_MAX_SCALE = 10  # our fractional-digit cap, leaving ample integer room


def _base_type_length(prop: PropertyMeta) -> str | None:
    """Render precision/scale as the ``length`` string for NUMERIC columns.

    Clamps to a Snowflake-valid ``NUMBER(precision, scale)`` (see module note
    above) so financial decimal columns load under authoritative data types.
    """
    if prop.base_type != "NUMERIC" or prop.precision is None:
        return None
    precision, scale = prop.precision, prop.scale
    if scale is None:
        return str(min(precision, _SF_MAX_PRECISION))
    if precision > _SF_MAX_PRECISION or scale > _SF_MAX_SCALE or scale > precision:
        precision = min(precision, _SF_MAX_PRECISION)
        scale = min(scale, _TARGET_MAX_SCALE, precision - 1)
    return f"{precision},{scale}"


def build_column_schema(
    entity_meta: EntityMeta,
    selected: list[str] | None,
    primary_key: list[str] | None = None,
    keep_meta_fields: bool = False,
) -> OrderedDict[str, ColumnDefinition]:
    """Build an authoritative ``{column: ColumnDefinition}`` schema.

    Honours ``$select`` (``selected``) and marks the chosen primary-key columns.
    Column order follows the EDMX metadata order, filtered by the selection.
    When ``keep_meta_fields`` is set, the IFS meta-fields (``@odata.etag`` etc.)
    are appended as trailing ``STRING`` columns so they actually reach the
    output CSV (the ``DictWriter`` fieldnames come from these schema keys).
    """
    pk = set(primary_key or [])
    selected_set = set(selected) if selected else None

    schema: OrderedDict[str, ColumnDefinition] = OrderedDict()
    for prop in entity_meta.properties:
        if selected_set is not None and prop.name not in selected_set:
            continue
        dtype = SupportedDataTypes(prop.base_type)
        schema[prop.name] = ColumnDefinition(
            data_types=BaseType(dtype=dtype, length=_base_type_length(prop)),
            nullable=prop.nullable,
            primary_key=prop.name in pk,
        )
    if keep_meta_fields:
        for name in META_FIELDS:
            if name in schema:
                continue
            schema[name] = ColumnDefinition(
                data_types=BaseType(dtype=SupportedDataTypes.STRING),
                nullable=True,
            )
    return schema


def format_filter_literal(edm_type: str, value: object) -> str:
    """Render ``value`` as an OData ``$filter`` literal for its EDM type.

    Strings are single-quoted (embedded quotes doubled per OData); booleans
    lower-cased; numbers, dates and timestamps emitted unquoted (OData v4 uses
    bare temporal literals, e.g. ``2025-01-01``).
    """
    base = edm_to_base_type(edm_type)
    if base == "STRING":
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    if base == "BOOLEAN":
        return "true" if str(value).strip().lower() in ("true", "1", "yes") else "false"
    return str(value)
