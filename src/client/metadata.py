"""EDMX ``$metadata`` parser and EDM -> Keboola base-type mapping.

Types are derived from the EDMX ``$metadata`` (OData CSDL v4), not the lossy
``$openapi`` (which collapses all numerics to ``number`` and drops nullability).
Uses stdlib :mod:`xml.etree.ElementTree` with ``{*}`` namespace wildcards so we
do not have to pin the exact EDMX/EDM namespace URIs.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# EDM primitive type -> Keboola authoritative base type (spec section 4.3).
_EDM_TO_BASE: dict[str, str] = {
    "Edm.String": "STRING",
    "Edm.Guid": "STRING",
    "Edm.Binary": "STRING",
    "Edm.Stream": "STRING",
    "Edm.Boolean": "BOOLEAN",
    "Edm.Byte": "INTEGER",
    "Edm.SByte": "INTEGER",
    "Edm.Int16": "INTEGER",
    "Edm.Int32": "INTEGER",
    "Edm.Int64": "INTEGER",
    "Edm.Decimal": "NUMERIC",
    "Edm.Double": "FLOAT",
    "Edm.Single": "FLOAT",
    "Edm.Date": "DATE",
    "Edm.DateTime": "TIMESTAMP",
    "Edm.DateTimeOffset": "TIMESTAMP",
    "Edm.Time": "STRING",
    "Edm.TimeOfDay": "STRING",
}

# Name substrings that hint a column is a change/entry timestamp (lower-cased).
_INCREMENTAL_NAME_HINTS = ("changed", "modified", "updated", "date", "time", "stamp", "created")

# IFS meta-fields that ride along on data rows but are not real data columns.
# Single source of truth (ordered) shared by the OData client (stripping) and
# the schema builder (rendered as trailing STRING columns when kept).
META_FIELDS: tuple[str, ...] = ("@odata.etag", "luname", "keyref", "Objgrants")


@dataclass
class PropertyMeta:
    name: str
    edm_type: str
    nullable: bool = True
    precision: int | None = None
    scale: int | None = None

    @property
    def base_type(self) -> str:
        return edm_to_base_type(self.edm_type)


@dataclass
class EntityMeta:
    entity_set: str
    entity_type: str
    properties: list[PropertyMeta] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    nav_properties: list[str] = field(default_factory=list)


def edm_to_base_type(edm_type: str) -> str:
    """Map an ``Edm.*`` type to a Keboola base type, defaulting to STRING."""
    return _EDM_TO_BASE.get(edm_type, "STRING")


def _local(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _short_type(qualified: str) -> str:
    """Return the local name of a qualified type (``IFS.Projections.X`` -> ``X``)."""
    return qualified.rsplit(".", 1)[-1]


def _parse_entity_type(node: ET.Element) -> tuple[str, list[PropertyMeta], list[str], list[str]]:
    name = node.get("Name", "")
    properties: list[PropertyMeta] = []
    keys: list[str] = []
    nav_properties: list[str] = []
    for child in node:
        local = _local(child.tag)
        if local == "Key":
            keys = [ref.get("Name", "") for ref in child if _local(ref.tag) == "PropertyRef"]
        elif local == "Property":
            precision = child.get("Precision")
            scale = child.get("Scale")
            properties.append(
                PropertyMeta(
                    name=child.get("Name", ""),
                    edm_type=child.get("Type", "Edm.String"),
                    nullable=child.get("Nullable", "true").lower() != "false",
                    precision=int(precision) if precision is not None else None,
                    scale=int(scale) if scale is not None else None,
                )
            )
        elif local == "NavigationProperty":
            nav_properties.append(child.get("Name", ""))
    return name, properties, keys, nav_properties


def parse_metadata(edmx_xml: str) -> dict[str, EntityMeta]:
    """Parse EDMX ``$metadata`` into ``{entity_set_name: EntityMeta}``."""
    root = ET.fromstring(edmx_xml)

    entity_types: dict[str, tuple[str, list[PropertyMeta], list[str], list[str]]] = {}
    entity_sets: list[tuple[str, str]] = []  # (entity_set_name, entity_type_local_name)

    for node in root.iter():
        local = _local(node.tag)
        if local == "EntityType":
            parsed = _parse_entity_type(node)
            entity_types[parsed[0]] = parsed
        elif local == "EntitySet":
            entity_sets.append((node.get("Name", ""), _short_type(node.get("EntityType", ""))))

    result: dict[str, EntityMeta] = {}
    for set_name, type_name in entity_sets:
        parsed = entity_types.get(type_name)
        if parsed is None:
            result[set_name] = EntityMeta(entity_set=set_name, entity_type=type_name)
            continue
        _, properties, keys, nav_properties = parsed
        result[set_name] = EntityMeta(
            entity_set=set_name,
            entity_type=type_name,
            properties=properties,
            keys=keys,
            nav_properties=nav_properties,
        )
    return result


def rank_incremental_fields(props: list[PropertyMeta]) -> list[str]:
    """Rank property names as Date Field candidates (date/timestamp columns).

    TIMESTAMP-typed columns first, then DATE-typed, then columns whose name
    hints at a change stamp, then everything else — original order preserved
    within each tier so the caller sees a stable, sensible dropdown.
    """

    def sort_key(indexed: tuple[int, PropertyMeta]) -> tuple[int, int]:
        idx, prop = indexed
        base = prop.base_type
        if base == "TIMESTAMP":
            tier = 0
        elif base == "DATE":
            tier = 1
        elif any(hint in prop.name.lower() for hint in _INCREMENTAL_NAME_HINTS):
            tier = 2
        else:
            tier = 3
        return (tier, idx)

    ranked = sorted(enumerate(props), key=sort_key)
    return [prop.name for _, prop in ranked]
