"""Authoritative output schema builder and type-aware ``$filter`` literals.

Pure functions (no I/O) so they unit-test without the platform. The schema is
the authoritative ``data_type.base.type`` form; ``format_filter_literal`` renders
a value into an OData ``$filter`` literal respecting its EDM type (used to build
the incremental ``gt`` watermark clause).
"""

from collections import OrderedDict

from keboola.component.dao import BaseType, ColumnDefinition, SupportedDataTypes

from client.metadata import EntityMeta, PropertyMeta, edm_to_base_type


def _base_type_length(prop: PropertyMeta) -> str | None:
    """Render precision/scale as the ``length`` string for NUMERIC columns."""
    if prop.base_type != "NUMERIC" or prop.precision is None:
        return None
    if prop.scale is not None:
        return f"{prop.precision},{prop.scale}"
    return str(prop.precision)


def build_column_schema(
    entity_meta: EntityMeta,
    selected: list[str] | None,
    primary_key: list[str] | None = None,
) -> OrderedDict[str, ColumnDefinition]:
    """Build an authoritative ``{column: ColumnDefinition}`` schema.

    Honours ``$select`` (``selected``) and marks the chosen primary-key columns.
    Column order follows the EDMX metadata order, filtered by the selection.
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
