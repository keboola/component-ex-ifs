"""IFS Cloud OData v4 extractor — component entrypoint.

``run()`` is a thin orchestrator delegating to private methods; the HTTP/auth
and metadata concerns live in the ``client`` package. One config row -> one
output table, with live EDMX schema discovery, native types, and a stateless,
customer-driven Date window (``date_field`` + ``date_start`` / ``date_end``)
that bounds the server-side ``$filter`` — recomputed each run, no stored cursor.
"""

import csv
import json
import logging
import os
import sys
from collections import OrderedDict
from datetime import UTC, datetime

import dateparser
from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import ColumnDefinition, TableDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult

from client.auth import IfsAuthClient
from client.metadata import EntityMeta, edm_to_base_type, parse_metadata, rank_incremental_fields
from client.odata import IfsODataClient
from client.schema import build_column_schema, format_filter_literal
from configuration import Configuration, RowConfiguration

logger = logging.getLogger(__name__)


# --- VCR cassette-recording sanitizers -------------------------------------
# ``VCR_SANITIZERS`` is picked up automatically by the keboola.datadirtest
# scaffolder when recording cassettes against the live IFS tenant. It has no
# effect on a normal platform run (secrets.json is absent there, so the rewrite
# rules resolve to an empty list and the sanitizers are never invoked).
#
# The real tenant leaked into a public repo on a prior build, so cassettes MUST
# NOT carry a real tenant host, realm, company code, service account or any real
# financial value. The rules below are read from the gitignored secrets.json so
# the recorder — not this transcript — sees the real values.

_IFS_PLACEHOLDERS = {
    "host": "acme.ifs.cloud",
    "tenant": "acme",
    "realm": "acme1",
    "service_account": "svc_account",
    "client_id": "acme_client",
}


def _ifs_rewrite_rules() -> list[tuple[str, str]]:
    """Return ``(real_value, placeholder)`` rewrite pairs read from secrets.json.

    Returns ``[]`` when no secrets file is present (CI replay / production run),
    which makes the host/realm rewrite a no-op — the committed cassette already
    holds placeholders and the replay config uses them.
    """
    for candidate in ("secrets.json", os.path.join(os.path.dirname(__file__), "..", "secrets.json")):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError, ValueError:
            return []
        params = data.get("parameters", data)
        rules: list[tuple[str, str]] = []
        tenant = str(params.get("tenant_id", "")).strip()
        realm = str(params.get("realm", "")).strip()
        service_account = str(params.get("service_account", "")).strip()
        client_id = str(params.get("client_id", "")).strip()
        if tenant:
            rules.append((f"{tenant}.ifs.cloud", _IFS_PLACEHOLDERS["host"]))
            rules.append((tenant, _IFS_PLACEHOLDERS["tenant"]))
        if realm:
            rules.append((realm, _IFS_PLACEHOLDERS["realm"]))
        if service_account:
            rules.append((service_account, _IFS_PLACEHOLDERS["service_account"]))
        if client_id:
            rules.append((client_id, _IFS_PLACEHOLDERS["client_id"]))
        # Longest match first so a tenant substring inside its own host is not
        # half-rewritten (host rule fires before the bare-tenant rule).
        return sorted(rules, key=lambda pair: len(pair[0]), reverse=True)
    return []


_IFS_REWRITE_RULES = _ifs_rewrite_rules()


def _ifs_scrub_text(text: str) -> str:
    for real, placeholder in _IFS_REWRITE_RULES:
        if real and real in text:
            text = text.replace(real, placeholder)
    return text


def _ifs_rewrite_request(request):
    """Rewrite the tenant host + realm in a recorded request URI (cassette-only)."""
    uri = getattr(request, "uri", None)
    if isinstance(uri, str):
        request.uri = _ifs_scrub_text(uri)
    return request


def _ifs_rewrite_response(response):
    """Rewrite the tenant host + realm inside a recorded response body (``@odata.context``)."""
    body = response.get("body") if isinstance(response, dict) else None
    if isinstance(body, dict) and "string" in body:
        payload = body["string"]
        if isinstance(payload, bytes):
            body["string"] = _ifs_scrub_text(payload.decode("utf-8", "ignore")).encode("utf-8")
        elif isinstance(payload, str):
            body["string"] = _ifs_scrub_text(payload)
    return response


try:
    from keboola.vcr import BodyFieldSanitizer, CallbackSanitizer, DefaultSanitizer
except ImportError:
    # keboola.vcr ships only via the dev extra (keboola.datadirtest); the
    # production image is built with `uv sync --no-dev`, so degrade to no-op there.
    VCR_SANITIZERS: list = []
else:
    VCR_SANITIZERS = [
        # 1. Auth/secret redaction, cassette-only: tokens + client_secret stay REAL
        #    for the component while recording, redacted in the written cassette.
        DefaultSanitizer(additional_sensitive_fields=["tenant_id", "realm"]),
        # 2. Host/realm rewrite, cassette-only (NOT scrub_before_read): the live
        #    host stays real for the component mid-record; only the cassette gets
        #    the acme.ifs.cloud / acme1 placeholders so replay matches the placeholder config.
        CallbackSanitizer(before_request=_ifs_rewrite_request, before_response=_ifs_rewrite_response),
        # 3. Financial / identifier scrub, scrub_before_read: the component READS
        #    scrubbed values, so committed cassettes AND expected/ tables carry no
        #    real financial data. Company -> the generic public code 100; the
        #    incremental date -> a fixed synthetic date; @odata.id (real PK/company
        #    key-predicate) and every other scalar column -> REDACTED. None of these
        #    are round-tripped to the API (no company $filter, $skip paging, no
        #    nextLink token), so redacting them before read cannot break recording.
        BodyFieldSanitizer(fields=["Company"], replacement="100", scrub_before_read=True),
        BodyFieldSanitizer(fields=["VoucherDate"], replacement="2020-01-01", scrub_before_read=True),
        BodyFieldSanitizer(
            fields=[
                "VoucherType",
                "AccountingYear",
                "VoucherNo",
                "RowNo",
                "Account",
                "Amount",
                "CurrencyCode",
                "@odata.id",
                "@odata.etag",
                "luname",
                "keyref",
                "Objgrants",
            ],
            replacement="REDACTED",
            scrub_before_read=True,
        ),
    ]


class Component(ComponentBase):
    """Generic, config-driven extractor for IFS Cloud projection services."""

    def __init__(self):
        super().__init__()
        self._config: Configuration | None = None
        self._auth: IfsAuthClient | None = None
        self._client: IfsODataClient | None = None
        self._init_clients()

    def _init_clients(self) -> None:
        """Build the auth + OData clients if the connection config is valid.

        Tolerates partial config (a sync action invoked before the connection
        is filled): leaves the clients unbuilt so those actions can degrade to a
        guidance item instead of crashing.
        """
        try:
            self._config = Configuration(**(self.configuration.parameters or {}))
        except UserException:
            self._config = None
            self._auth = None
            self._client = None
            return
        advanced = self._config.advanced
        self._auth = IfsAuthClient(
            host=self._config.host,
            realm=self._config.realm,
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            timeout=advanced.request_timeout,
        )
        self._client = IfsODataClient(
            host=self._config.host,
            base_path=advanced.base_path,
            auth=self._auth,
            page_size=advanced.page_size,
            timeout=advanced.request_timeout,
            max_retries=advanced.max_retries,
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
        meta = self._discover(row)
        query_filter = self._build_filter(row, meta)
        table, count = self._extract(row, meta, query_filter)
        self._finalize(table, count)
        self._log_extract_result(row, count)

    @staticmethod
    def _log_extract_result(row: RowConfiguration, count: int) -> None:
        if count == 0:
            logger.warning(
                "Extracted 0 rows from %s/%s. IFS data is company-scoped — confirm the intended "
                "companies are in the service account's Allowed Companies inside IFS. An empty result "
                "is not an error.",
                row.service,
                row.entity_set,
            )
        else:
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

    def _build_filter(self, row: RowConfiguration, meta: EntityMeta) -> str | None:
        """Combine the user ``$filter`` with the customer-driven Date window.

        The window is stateless — recomputed each run from the config, never from
        a stored cursor:
        - ``date_start`` set -> ``{date_field} ge {resolved_start}`` (lower bound).
        - ``date_end`` set -> ``{date_field} lt {resolved_end}`` (upper bound).

        Both bounds require ``date_field`` (enforced in ``RowConfiguration``) and
        are ANDed with the user ``filter``. ``date_start``/``date_end`` accept
        relative (``5 days ago``, ``today``) or absolute (``YYYY-MM-DD`` / ISO)
        values, resolved to a concrete UTC value at run time and formatted for the
        field's EDM type. An empty window fetches everything.
        """
        clauses: list[str] = []
        if row.filter:
            clauses.append(row.filter)
        date_field = row.date_field
        if date_field:
            edm_type = self._edm_type_of(meta, date_field)
            if row.date_start:
                clauses.append(f"{date_field} ge {self._resolve_bound_literal(row.date_start, edm_type)}")
            if row.date_end:
                clauses.append(f"{date_field} lt {self._resolve_bound_literal(row.date_end, edm_type)}")
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return " and ".join(f"({clause})" for clause in clauses)

    @staticmethod
    def _resolve_bound_literal(raw: str, edm_type: str) -> str:
        """Resolve a relative/absolute ``date_start``/``date_end`` to a filter literal.

        The value is parsed (relative anchored to ``now`` in UTC) then rendered in
        the shape the Date Field's EDM type expects (``YYYY-MM-DD`` for a date,
        ISO-8601 ``…Z`` for a timestamp) before ``format_filter_literal`` applies
        OData quoting rules.
        """
        base = datetime.now(tz=UTC).replace(tzinfo=None)
        parsed = dateparser.parse(
            raw,
            settings={"RELATIVE_BASE": base, "TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": False},
        )
        if parsed is None:
            raise UserException(
                f"Could not parse the date value '{raw}'. Use a relative value such as "
                "'5 days ago', 'yesterday' or 'today', or an absolute date like 'YYYY-MM-DD'."
            )
        base_type = edm_to_base_type(edm_type)
        if base_type == "TIMESTAMP":
            value = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            value = parsed.date().isoformat()
        return format_filter_literal(edm_type, value)

    def _extract(
        self, row: RowConfiguration, meta: EntityMeta, query_filter: str | None
    ) -> tuple[TableDefinition, int]:
        """Stream rows to CSV, returning the table definition and row count."""
        selected = self._effective_select(row)
        schema: OrderedDict[str, ColumnDefinition] = build_column_schema(
            meta, selected, primary_key=row.primary_key, keep_meta_fields=row.keep_meta_fields
        )
        table = self.create_out_table_definition(
            name=f"{row.table_name}.csv",
            schema=schema,
            primary_key=row.primary_key,
            incremental=row.incremental,
            has_header=True,
        )
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
                strip_meta=not row.keep_meta_fields,
            ):
                writer.writerow(record)
                count += 1
        return table, count

    @staticmethod
    def _effective_select(row: RowConfiguration) -> list[str] | None:
        """Compute the effective ``$select`` sent to extraction.

        An empty ``columns`` means "all columns" (``None``); otherwise the user's
        chosen columns are sent verbatim. The Date Field need not be selected —
        OData ``$filter`` may reference a column outside ``$select``.
        """
        return list(row.columns) if row.columns else None

    def _finalize(self, table: TableDefinition, count: int) -> None:
        """Write the manifest and an operational state file (no data cursor)."""
        self.write_manifest(table)
        self.write_state_file(
            {
                "last_run": datetime.now(tz=UTC).isoformat(),
                "records_extracted": count,
            }
        )

    # --- sync actions --------------------------------------------------
    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        """Validate the connection by exchanging a token."""
        self._require_auth().get_token()
        return ValidationResult("Successfully connected to IFS.", MessageType.SUCCESS)

    def _require_auth(self) -> IfsAuthClient:
        """Return a built auth client, raising a clear error if config is incomplete."""
        self._require_config()
        if self._auth is None:
            self._init_clients()
        if self._auth is None:  # pragma: no cover - config validated just above
            raise UserException("The connection is not configured. Fill in tenant, realm, client id and secret.")
        return self._auth

    @sync_action("validate_query")
    def validate_query(self) -> ValidationResult:
        """Validate a row's assembled OData query with a cheap ``$top=1`` GET."""
        params = self.configuration.parameters or {}
        service = params.get("service")
        entity_set = params.get("entity_set")
        if self._client is None or not service or not entity_set:
            return ValidationResult("Fill in the connection, service and entity set first.", MessageType.WARNING)
        ok, message = self.client.validate_query(
            service,
            entity_set,
            select=params.get("columns") or None,
            filter=params.get("filter") or None,
            orderby=params.get("order_by") or None,
        )
        return ValidationResult(message, MessageType.SUCCESS if ok else MessageType.ERROR)

    @sync_action("list_services")
    def list_services(self) -> list[SelectElement]:
        """Populate the service dropdown from the tenant projection catalog."""
        if self._client is None:
            return [self._guidance("Fill in the connection first, then reload.")]
        try:
            projections = self.client.list_projections()
        except UserException:
            return [self._guidance("Catalog unavailable — type the service name manually.")]
        items = [
            SelectElement(value=name, label=name) for name in (self._projection_name(p) for p in projections) if name
        ]
        return items or [self._guidance("No services returned — type the service name manually.")]

    @sync_action("list_entitysets")
    def list_entitysets(self) -> list[SelectElement]:
        """Populate the entity-set dropdown from the service metadata."""
        service = (self.configuration.parameters or {}).get("service")
        if self._client is None or not service:
            return [self._guidance("Select a service first.")]
        entity_sets = parse_metadata(self.client.get_metadata(service))
        return [SelectElement(value=name, label=name) for name in sorted(entity_sets)]

    @sync_action("list_columns")
    def list_columns(self) -> list[SelectElement]:
        """Populate the column ($select) dropdown with scalar EDMX properties only.

        Navigation properties are intentionally excluded — OData ``$select``
        rejects them with a 400.
        """
        meta = self._selected_entity_meta()
        if meta is None:
            return [self._guidance("Select a service and entity set first.")]
        return [SelectElement(value=p.name, label=p.name) for p in meta.properties]

    @sync_action("list_primary_keys")
    def list_primary_keys(self) -> list[SelectElement]:
        """Populate the primary-key dropdown, EDMX key columns ranked first."""
        meta = self._selected_entity_meta()
        if meta is None:
            return [self._guidance("Select a service and entity set first.")]
        ranked = meta.keys + [p.name for p in meta.properties if p.name not in meta.keys]
        return [SelectElement(value=name, label=name) for name in ranked]

    @sync_action("list_date_fields")
    def list_date_fields(self) -> list[SelectElement]:
        """Populate the Date Field dropdown, date/timestamp columns ranked first."""
        meta = self._selected_entity_meta()
        if meta is None:
            return [self._guidance("Select a service and entity set first.")]
        return [SelectElement(value=name, label=name) for name in rank_incremental_fields(meta.properties)]

    def _selected_entity_meta(self) -> EntityMeta | None:
        # Raw .get() (not a parsed RowConfiguration) is deliberate: discovery runs on
        # in-progress config where required row fields (entity_set, PK) aren't set yet.
        params = self.configuration.parameters or {}
        service = params.get("service")
        entity_set = params.get("entity_set")
        if self._client is None or not service or not entity_set:
            return None
        return parse_metadata(self.client.get_metadata(service)).get(entity_set)

    @staticmethod
    def _projection_name(projection: dict) -> str | None:
        for key in ("Name", "name", "Projection", "ProjectionName", "EntityName", "Service"):
            value = projection.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _guidance(label: str) -> SelectElement:
        """A non-selectable dropdown item used when a precondition is missing."""
        return SelectElement(value="", label=label)


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
