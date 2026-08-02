"""IFS Cloud OData v4 extractor — component entrypoint.

``run()`` is a thin orchestrator delegating to private methods; the HTTP/auth
and metadata concerns live in the ``client`` package. One config row -> one
output table, with live EDMX schema discovery, native types, and an incremental
``gt`` watermark persisted to ``state.json`` after a successful write.
"""

import csv
import logging
import sys
from collections import OrderedDict
from datetime import UTC, datetime

from keboola.component.base import ComponentBase
from keboola.component.dao import ColumnDefinition, TableDefinition
from keboola.component.exceptions import UserException

from client.auth import IfsAuthClient
from client.metadata import EntityMeta, parse_metadata
from client.odata import IfsODataClient
from client.schema import build_column_schema, format_filter_literal
from configuration import Configuration, FetchType, RowConfiguration

logger = logging.getLogger(__name__)


class Component(ComponentBase):
    """Generic, config-driven extractor for IFS Cloud projection services."""

    def __init__(self):
        super().__init__()
        self._config: Configuration | None = None
        self._client: IfsODataClient | None = None
        self._init_clients()

    def _init_clients(self) -> None:
        """Build the auth + OData clients if the connection config is valid.

        Tolerates partial config (a sync action invoked before the connection
        is filled): leaves the client unbuilt so those actions can degrade to a
        guidance item instead of crashing.
        """
        try:
            self._config = Configuration(**(self.configuration.parameters or {}))
        except UserException:
            self._config = None
            return
        auth = IfsAuthClient(
            host=self._config.host,
            realm=self._config.realm,
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            timeout=self._config.request_timeout,
        )
        self._client = IfsODataClient(
            host=self._config.host,
            base_path=self._config.base_path,
            auth=auth,
            page_size=self._config.page_size,
            timeout=self._config.request_timeout,
            max_retries=self._config.max_retries,
        )

    @property
    def client(self) -> IfsODataClient:
        if self._client is None:
            raise UserException("The connection is not configured. Fill in tenant, realm, client id and secret.")
        return self._client

    def run(self) -> None:
        """Extract one entity set into one output table (thin orchestrator)."""
        self._require_config()
        row = self._parse_row()
        previous_state = self.get_state_file() or {}
        meta = self._discover(row)
        query_filter = self._build_filter(row, meta, previous_state)
        table, watermark, count = self._extract(row, meta, query_filter)
        self._finalize(row, table, watermark, count, previous_state)
        logger.info("Extracted %d rows from %s/%s.", count, row.service, row.entity_set)

    def _require_config(self) -> Configuration:
        if self._config is None:
            self._config = Configuration(**(self.configuration.parameters or {}))
        return self._config

    def _parse_row(self) -> RowConfiguration:
        return RowConfiguration(**(self.configuration.parameters or {}))

    def _discover(self, row: RowConfiguration) -> EntityMeta:
        entity_sets = parse_metadata(self.client.get_metadata(row.service))
        meta = entity_sets.get(row.entity_set)
        if meta is None:
            raise UserException(
                f"Entity set '{row.entity_set}' was not found in service '{row.service}'. "
                f"Available: {', '.join(sorted(entity_sets)) or '(none)'}."
            )
        return meta

    @staticmethod
    def _edm_type_of(meta: EntityMeta, field: str) -> str:
        for prop in meta.properties:
            if prop.name == field:
                return prop.edm_type
        return "Edm.String"

    def _build_filter(self, row: RowConfiguration, meta: EntityMeta, state: dict) -> str | None:
        """Combine the user ``$filter`` with the incremental ``gt`` watermark."""
        clauses: list[str] = []
        if row.filter:
            clauses.append(row.filter)
        last_value = state.get("last_value")
        inc_field = row.incremental_field
        if row.fetch_type == FetchType.incremental_fetch and inc_field and last_value is not None:
            literal = format_filter_literal(self._edm_type_of(meta, inc_field), last_value)
            clauses.append(f"{inc_field} gt {literal}")
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return " and ".join(f"({clause})" for clause in clauses)

    def _extract(
        self, row: RowConfiguration, meta: EntityMeta, query_filter: str | None
    ) -> tuple[TableDefinition, object | None, int]:
        """Stream rows to CSV, tracking the max incremental_field value."""
        selected = row.columns or None
        schema: OrderedDict[str, ColumnDefinition] = build_column_schema(meta, selected, primary_key=row.primary_key)
        table = self.create_out_table_definition(
            name=f"{row.entity_set}.csv",
            schema=schema,
            primary_key=row.primary_key,
            incremental=row.incremental,
            has_header=True,
        )
        watermark_field = row.incremental_field if row.fetch_type == FetchType.incremental_fetch else None
        watermark: object | None = None
        count = 0
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=list(schema.keys()), extrasaction="ignore")
            writer.writeheader()
            for record in self.client.iter_rows(
                row.service,
                row.entity_set,
                select=selected,
                filter=query_filter,
                orderby=row.order_by,
                expand=row.expand,
                strip_meta=not row.keep_meta_fields,
            ):
                writer.writerow(record)
                count += 1
                watermark = self._advance_watermark(watermark, record, watermark_field)
        return table, watermark, count

    @staticmethod
    def _advance_watermark(current: object | None, record: dict, field: str | None) -> object | None:
        if field is None:
            return current
        value = record.get(field)
        if value is None:
            return current
        if current is None or value > current:
            return value
        return current

    def _finalize(
        self,
        row: RowConfiguration,
        table: TableDefinition,
        watermark: object | None,
        count: int,
        previous_state: dict,
    ) -> None:
        """Write the manifest, then persist the advanced watermark state."""
        self.write_manifest(table)
        state: dict = {
            "last_run": datetime.now(tz=UTC).isoformat(),
            "records_extracted": count,
        }
        if row.fetch_type == FetchType.incremental_fetch:
            state["last_value"] = watermark if watermark is not None else previous_state.get("last_value")
        self.write_state_file(state)


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException:
        logger.exception("Component failed with a user error")
        sys.exit(1)
    except Exception:
        logger.exception("Component failed with an unexpected error")
        sys.exit(2)
