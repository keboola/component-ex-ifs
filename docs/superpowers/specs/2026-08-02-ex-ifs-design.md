# keboola.ex-ifs — Design Spec

> Type: extractor
> Component ID: keboola.ex-ifs
> Status: draft
> Date: 2026-08-02

## 1. Overview & source system

`keboola.ex-ifs` is a single, generic, config-driven Keboola extractor for **IFS Cloud OData v4
projection services**. A user points it at their IFS Cloud tenant and extracts any projection
service / entity set as a Keboola table, with live schema discovery, incremental loading, and native
data types — one component, N config rows, no per-service code.

- **Source system:** IFS Cloud — projection (OData v4) APIs, fronted by Keycloak for OAuth 2.0.
  - Projection API docs: https://docs.ifs.com/techdocs/24r2/040_tailoring/300_extensibility/020_api_explorer/040_entity_services/
  - Auth (IAM Clients / OAuth2): https://docs.ifs.com/techdocs/foundation1/010_overview/210_security/030_authentication/oauth2.htm
  - OData v4: https://docs.oasis-open.org/odata/odata/v4.01/
- **Domain-neutral by design.** The connector targets any projection service equally — no data domain
  is privileged. `VoucherRowsAnalysis` (`VoucherRowSet`) appears throughout as **one example / test
  case** (chosen only because its OpenAPI is already on disk) — a projection with a composite key and
  ~89 flat primitive fields; its business domain is treated as illustrative, not asserted, and it is
  not a primary or canonical fixture. The same machinery serves every other projection unchanged.

IFS Cloud exposes thousands of projection services (a live tenant enumerated ~5,640). They come in
API classes — **Standard** (IFS-supported public surface, stable schemas) and user-generated
**custom / QuickReport** services (unstable schemas). Hand-building an extractor per service does not
scale; this component discovers and extracts them by configuration instead.

## 2. Keboola mapping

How IFS concepts map onto how Keboola runs the component:

| IFS concept | Keboola construct |
|---|---|
| Tenant connection + Keycloak auth | **Root config** parameters (entered once) |
| One projection service + entity set to extract | **One config row** → one output table |
| OData collection response (`value[]`) | Rows of one output table |
| EDMX `$metadata` column types | Native output-table `schema` (base types) |
| OData `@odata.nextLink` server paging | Internal page loop (not a user concern) |
| Per-service catalog / metadata | Sync-action dropdowns |
| Watermark column (`gt` filter) | `state.json` watermark + incremental output mapping |

- **Config rows, one row per extracted table** — per Keboola convention for multiple independent
  objects. Each row = `service` + `entity_set` + query options (`$select`/`$filter`/`$orderby`) +
  fetch/load options + PK + incremental column. Rows can be enabled, scheduled, run and retried
  independently, and each gets its own `state.json`. A single-config layout is rejected: a tenant has
  many tables, and a multipick would collapse them into one shared-state run.
- **Rows run sequentially by default** (platform behaviour). Per-row parallelism is opt-in via the
  platform `parallelism` setting; the component does not assume or require it, and never shares state
  across rows. The component always receives a single already-merged `config.json` (root + row).
- **Two independent load axes** (mirrors the SAP OData sibling):
  - `fetch_type` = `full_fetch` | `incremental_fetch` → controls the OData `$filter` watermark.
  - `load_type` = `full_load` | `incremental_load` → controls the Keboola Storage write mode
    (`incremental=True` + primary key = upsert).
- **Incremental strategy → state:** on an incremental row the component reads the stored watermark,
  adds `$filter {incremental_field} gt {last_value}` (strictly greater-than — `gt`, not `ge`, to
  avoid re-emitting the boundary row; Keboola PK-dedup absorbs any residual overlap), streams the
  result, tracks the **max value of the `incremental_field` column across the written rows**, and
  persists that max **after** a successful write. `state.json` is row-scoped and holds
  `{ "last_run": "<ISO-8601 UTC>", "last_value": "<max value of incremental_field>", "records_extracted": N }`.
  First run (empty state) falls back to full fetch (or an optional configured seed value). See §5.
  - **Deliberate divergence from the canonical wall-clock watermark:** the standard `last_run`
    pattern (capture `now()` before fetch, filter server-side by "changed since `now`") does **not**
    apply here, because IFS has no generic "changed-since" server signal — the filter compares an
    actual **data column** (`incremental_field`, e.g. `EntryDate`), so the stored bound must be a
    value from that column's own domain (a date/timestamp/id), not wall-clock time (which is not
    comparable to, say, a business-date or integer-id column). Hence the watermark is the max
    `incremental_field` value among successfully-written rows, persisted only after the write (a
    failed run keeps the prior bound and is safely retried). `last_run` is stored alongside for
    operator visibility only, not used as the filter bound. Where `incremental_field` is day-grain
    (`Edm.Date`), the `gt` boundary can miss same-day late arrivals — documented; prefer a true
    change-stamp column where the projection exposes one, and rely on PK upsert to absorb overlap.
- **Secrets → `#`-prefixed keys:** `#client_secret` (encrypted, `KBC::ProjectSecure`).
- **Sync actions** (validate + drive dropdowns in the UI): `testConnection`, `validate_query`,
  `list_services`, `list_entitysets`, `list_columns`, `list_primary_keys`, `list_incremental_fields`.
  See §5.
- **Output naming:** one stable output table per row, named from the row (default `{entity_set}` →
  `{service}_{entity_set}` when disambiguation is needed) — never generated at runtime. Storage
  destination resolves via the component's default-bucket behaviour (`in.c-keboola.ex-ifs-{configId}`);
  the component does not hard-code bucket names.

## 3. Authentication & connection

- **Method: OAuth 2.0 `client_credentials`** (Keycloak-fronted, machine-to-machine) — the proven,
  unattended path and **the only grant this component implements**. Auth is a single concrete client:
  no `auth_type` switch, no multi-grant abstraction. Alternatives were considered and are not built:
  `password` (ROPC) is not implemented (§4.2); `authorization_code`+PKCE is interactive (the IFS web
  client) and unusable headlessly; basic auth is disabled by default on the tenant.
- **Token exchange (proven end-to-end this build):**
  - Endpoint: `https://{host}/auth/realms/{realm}/protocol/openid-connect/token`, where
    `host = {tenant_id}.ifs.cloud` (derived from config) and `realm` is a config value.
  - Client authentication method: **`client_secret_post`** (`client_id` + `client_secret` in the POST
    body, `application/x-www-form-urlencoded`).
  - Request scope: `openid microprofile-jwt` (`microprofile-jwt` carries the IFS identity claims; the
    granted scope observed on the tenant may be broader, e.g. `openid audience microprofile-jwt profile
    email` — accept whatever is granted).
  - **Token TTL is short (observed 180 s).** This is an explicit design requirement: the client MUST
    cache the token to its `expires_in` and **refresh mid-run** with a safety margin (re-exchange when
    the remaining lifetime drops below a small threshold, e.g. 30 s, and on any `401`). A long
    extraction outlives a single token, so a fetch-once-at-start token will fail partway. Never
    hard-code the TTL — read `expires_in` from the token response.
- **Config / connection shape** (root config, Pydantic model) — the entirety of what the component
  needs to authenticate:
  - `tenant_id` (string, required) → `host = {tenant_id}.ifs.cloud`.
  - `realm` (string, required) — Keycloak realm; discoverable at
    `https://{host}/auth/realms/{realm}/.well-known/openid-configuration` if it ever needs verifying.
  - `client_id` (string, required).
  - `#client_secret` (encrypted string, required).
  - `service_account` (string, optional) — the IFS service-user identity behind the client; recorded
    for operator clarity, not sent in the token request.
  - Optional advanced defaults: `base_path` (default `/main/ifsapplications/projection/v1`),
    `page_size` (default 1000), request timeout, max retries.
- **What the component does NOT manage — server-side access control.** IFS data access is governed by
  IFS **Permission Sets / roles** and **company-level row security**, both attached to the service
  account **inside IFS** (see §3.1). These are **not component configuration** and the component never
  reads, sets, or reasons about them — it presents a token and receives exactly what that service
  account is permitted to see. A `403` or an empty result is surfaced as a **diagnostic** (§6), not
  something the component controls.

### 3.1 Prerequisites — IFS admin setup (external, server-side; NOT component config)

One-time steps an IFS admin performs **inside IFS** on the service account. They determine what the
authenticated account may see; they are **not** component config fields and the component neither reads
nor manages them. Summarize in the setup docs; full runbook in Phase 6. In **Solution Manager → Users
and Permissions → Identity and Access Manager → IAM Client**:

1. Create a confidential IAM Client — set `Client ID` (≤20 chars), **uncheck `Public Client`** (makes
   it confidential → yields a Client Secret), **enable `Service Accounts`** (turns on the
   `client_credentials` grant).
2. Create the IFS Service User via the IAM Client's **`Username` field** — on save IFS auto-creates the
   service user. (Manual Users creation cannot match the token's long service-account identity:
   Identity ≤20 chars and the Directory ID then locks.)
3. Grant the service user the End User Role **`IFSREADONLYSUPPORT`** ("Read-Only Grants for all
   Projections") — proven **sufficient alone** for run-application + projection read;
   `FND_WEBENDUSER_MAIN` is **not** needed. **Least privilege:** grant no role beyond this.
4. **Company-scoped data (row-level security):** add the companies to be pulled to the service user's
   **Allowed Companies**. Data returns empty until at least one company is granted.
5. Hand the component operator only: `realm`, `client_id`, `client_secret`, and the granted company
   code(s) — these are the sole values that become component config.

- **Blockers / access:** the only gate is this server-side provisioning (IAM Client + grants + at least
  one Allowed Company) on the customer tenant (see §9) — external to the component. All contract facts
  (endpoints, query options, response shape, field types) are resolved from the on-disk OpenAPI/EDMX
  and public docs and are not creds-gated. Recording real cassettes (Phase 5) and the cf-dev smoke test
  (Phase 7) require live credentials.

## 4. Capability inventory & scope

Because the component is generic, the "source surface" is the **OData query surface itself**,
enumerated live per service via `$metadata` — plus the tenant-level catalog, the auth grants, and the
non-OData surfaces IFS also exposes. Every capability from Phase 2 research appears below with an
explicit verdict. Excluded rows name a real reason **and** carry the user's sign-off (decisions D2/D3
and the YAGNI list are the user's explicit scope calls).

### 4.1 Data / extraction surface (per projection service)

| Capability | Verdict | Rationale |
|---|---|---|
| **Standard-class projection services** (default menu) | In scope | Stable, IFS-supported surface; the default target (D2). Enumerated via `AllProjections` + free-text. |
| **Custom / QuickReport projection services** (opt-in) | In scope | D2: user explicitly types a named custom service. Not blanket-defaulted (unstable schemas), but fully supported on demand via per-service `$metadata`. |
| **Main entity-set collection reads** (`GET {svc}/{EntitySet}`) | In scope | The core extract — one entity set per config row → one table. Example: `VoucherRowSet`. |
| **Reference / lookup entity sets** (e.g. `Reference_Account`, `Reference_CodeB..J`, `Reference_VoucherType`, `Reference_TaxBookLov`, `Reference_TaxSeries`, `Reference_UserGroupFinance`, `Reference_DeliveryType`) | In scope | Discovered like any entity set; each extractable as its own config row (dimension tables). No special-casing needed — they are entity sets. |
| **Navigation properties / `$expand` denormalization** (e.g. `AccountRef`, `VoucherTypeRef`) | In scope (optional, default off) | Offered as an opt-in per-row `$expand` toggle (YAGNI: not default denormalization). `list_columns` can surface nav-props; expanded objects flatten to prefixed scalar columns. |
| **OData query options** — `$filter`, `$select`, `$orderby`, `$top`, `$skip`, `$count`, `$expand` | In scope | `$select` (payload trim + column pick), `$filter` (incremental + user filter), `$orderby`, `$top`/`$skip` (paging fallback), `$count` (best-effort; some projections reject it — never make paging depend on it), `$expand` (above). |
| **`$search` free-text option** | In scope (best-effort) | Declared in the spec but not attached to every operation; exposed as an optional row filter, not relied on. |
| **`$apply` (aggregation) option** | Excluded | Not modeled by IFS projections; aggregation belongs in a downstream transformation, not the extractor. User-approved YAGNI. |
| **Catalog enumeration** (`AllProjections.svc/Projections`) | In scope | Powers the `list_services` sync-action dropdown (~5,640 services → searchable/typeahead), with **free-text service name as the always-works fallback** (per-service `$metadata` works without the catalog). |
| **Per-service metadata** (`$metadata` EDMX, `$openapi?V3`) | In scope | Drives `list_entitysets` / `list_columns` and type mapping. **Types are derived from EDMX `$metadata`**, not the lossy `$openapi` (which collapses all numerics to `number` and all dates to `date`, and emits no `nullable`). |
| **By-key single-entity reads** (`GET {svc}/{EntitySet}(key=…)`) | Excluded | A single-record lookup mechanism, not a bulk data family; an extractor pulls collections. Used internally only for optional preview, not as an extraction mode. User-approved. |
| **Bound function `GetMultiCompanyInfo`** | Excluded (deferred) | A parameterized single-record function (needs a specific voucher key), not a bulk collection — no config-row/table shape. Recorded 2026-08-02, user-approved; revisit if a real need appears. |
| **Bound function `InitLocalizationFunctionalities`** | Excluded | ~100 country-localization config flags — UI/config-gating metadata, not data (explicit YAGNI in the design). User-approved. |
| **IFS meta-fields on data rows** (`@odata.etag`, `luname`, `keyref`, `Objgrants`) | In scope (handled, stripped by default) | Not real data columns; stripped from output by default with an optional per-row keep toggle. Design + impl note, not a data family. |
| **Write-back to IFS** (create/update/delete) | Excluded | This is an extractor; the reference projections are read-only (`GET`-only). User-approved YAGNI. |
| **Non-OData IFS surfaces** (SOAP, file/bulk, aurena internal) | Excluded | Out of scope by design (D1: OData projections only). User-approved YAGNI. |

### 4.2 Authentication surface

| Auth capability | Verdict | Rationale |
|---|---|---|
| **OAuth 2.0 `client_credentials`** (Keycloak) | In scope | The proven unattended path; the only grant this component implements. |
| **OAuth 2.0 `password` (ROPC / Direct Access Grant)** | **Excluded — not planned (user decision)** | `client_credentials` only. No provisionable public ROPC client was confirmed on the tenant, and the maintainer decided `password` will not exist — no `auth_type` switch and no seam are built for it. |
| **OAuth 2.0 `authorization_code` + PKCE** | Excluded | Interactive browser flow (the IFS web client `IFS_aurena`); unusable for an unattended extractor. User-approved. |
| **HTTP Basic auth** | Excluded | Declared in the projection spec but disabled by default on IFS tenants; not a reliable path. User-approved. |

### 4.3 In-scope mechanics

- **Pagination:** server-driven via `@odata.nextLink` — follow it verbatim (it carries an opaque
  `$skiptoken`) until absent. Fallback: if no `nextLink` but a page returns exactly `page_size` rows,
  client-driven `$skip` until a short/empty page. `@odata.nextLink` is **runtime-only** (not in the
  OpenAPI). A hard safety cap (e.g. 100k pages) prevents an infinite loop. Explicit stopping condition
  required. `$count` (`$count=true`) is requested only when genuinely needed and never gates paging.
- **Rate limits:** none published for IFS projections. The component uses a bounded retry with
  backoff on `429`/`5xx` and respects `Retry-After` if present; otherwise it paces naturally through
  sequential paging.
- **Bulk/async export:** none — IFS projections are synchronous OData reads. N/A.
- **Response shapes / nested data:** collections return `{ "value": [ … ] }`; a by-key GET returns a
  bare object. Reference `VoucherRow` is 89 flat primitive fields (no nested objects). Where `$expand`
  is used, expanded entities flatten to prefixed scalar columns; a variable-length expanded collection
  (if ever present) may stay a single JSON column. IFS meta-fields are stripped (§4.1).
- **Type mapping (EDMX-driven):** `Edm.String/Guid/Binary/Stream → STRING`; `Edm.Boolean → BOOLEAN`;
  `Edm.Int16/Int32/Int64/Byte/SByte → INTEGER`; `Edm.Decimal → NUMERIC` (precision/scale from
  metadata); `Edm.Double/Single → FLOAT`; `Edm.Date → DATE`; `Edm.DateTime/DateTimeOffset → TIMESTAMP`;
  `Edm.Time/TimeOfDay → STRING`; enums → `STRING`; unknown → `STRING` (fallback).

## 5. Configuration & schema

The actual `configSchema.json` / `configRowSchema.json` are built by **`component-build-ui`** (see the
plan). This section describes the fields and behaviours.

**Root config (connection) — `configSchema.json`:**
- `tenant_id` — required, string. Title "Tenant ID"; tooltip explains `host = {tenant_id}.ifs.cloud`.
- `realm` — required, string. Keycloak realm.
- `client_id` — required, string.
- `#client_secret` — required, encrypted (alias `#client_secret`). Rendered as password field.
- `service_account` — optional, string.
- Advanced (collapsed section): `base_path`, `page_size`, timeout, retries — all with defaults.
- `testConnection` — `format: "test-connection"` widget; validates the token exchange.

**Row config (one table) — `configRowSchema.json`:**
- `service` — string. Async dropdown fed by `list_services` (autoload), **free-text allowed** as the
  fallback for custom/uncatalogued services (D2).
- `entity_set` — string. Async dropdown fed by `list_entitysets` (depends on `service`).
- `columns` (`$select`) — optional multi-select fed by `list_columns` (depends on `service` +
  `entity_set`); empty = all columns.
- `primary_key` — multi-select fed by `list_primary_keys` (ranked from the EDMX key). Required when
  `load_type = incremental_load`.
- `fetch_type` — enum `full_fetch` | `incremental_fetch` (default `full_fetch`).
- `incremental_field` — single-select fed by `list_incremental_fields` (date/timestamp columns ranked
  first). Required when `fetch_type = incremental_fetch`. Shown only for `incremental_fetch`
  (`options.dependencies`).
- `load_type` — enum `full_load` | `incremental_load` (default `incremental_load`, CF default).
- `filter` (`$filter`) — optional free-text OData filter (user-supplied, combined with the watermark).
- `order_by` (`$orderby`) — optional.
- `expand` (`$expand`) — optional; opt-in denormalization toggle/field (default off).
- `keep_meta_fields` — optional boolean (default false) → strip IFS meta-fields unless set.
- `validate_query` — a validate button (`format` sync-action widget) that runs the `validate_query`
  sync action against the row's current `service` / `entity_set` / query options (see Sync actions).

**Schema conventions to honour** (checked by the schema-ui gate): conditional fields via
`options.dependencies` (not root-level `dependencies`); async selects carry `"enum": []`; every
`options.async.action` has a matching `@sync_action`; required declared via parent `"required": [...]`;
`#`-field names match the Pydantic `alias=`; the first async dropdown autoloads; fields grouped into
named `type: object` sections (connection / advanced on root; service+entity / query / incremental on
the row) rather than one flat 6+ field list; `enum` fields carry `options.enum_titles`; titles Title
Case, descriptions Sentence case, all English.

**Sync actions** (read-only; return `[{"label","value"}]` for dropdowns; `testConnection` raises
`UserException` on failure, never on success):
- `testConnection` — exchange a token (and optionally one trivial catalog GET) to validate creds.
- `validate_query` — validate a row's assembled OData query **before** a full run (especially for
  custom / free-text services, which have no dropdown safety net). Issues one cheap
  `GET {svc}/{EntitySet}?$top=1` with the row's `$select`/`$filter`/`$orderby` applied: HTTP 200 →
  return a "query valid" message; `400` or other OData error → return the OData `error.message` text
  (malformed `$filter`, unknown column/entity set) as a readable failure. Raises `UserException` only
  on a genuine transport error, not on a validation failure.
- `list_services` — `GET AllProjections.svc/Projections`, map to dropdown; free-text fallback if the
  catalog is unavailable. Missing/invalid connection → return a guidance item `{"value":"","label":…}`
  (exit 0), not a crash.
- `list_entitysets` — parse the service `$metadata` (EDMX) → entity sets.
- `list_columns` — parse `$metadata` → properties (+ nav-props for `$expand`).
- `list_primary_keys` — EDMX entity `Key` → ranked PK candidates.
- `list_incremental_fields` — properties ranked by datetime type + name heuristics (e.g. `EntryDate`).

Every `@sync_action` must also be **registered in the Developer Portal** (Phase 6) or it 404s
on-platform regardless of image tag.

## 6. Code architecture

Modules (API/auth client separated from `component.py`; `run()` a thin orchestrator):

- `src/client/auth.py` — `IfsAuthClient` (single concrete class; no ABC, no multi-grant seam). Owns
  the Keycloak `client_credentials` token exchange (`client_secret_post`, scope
  `openid microprofile-jwt`), token caching to `expires_in`, and **mid-run refresh** (refresh under a
  safety margin and on `401`).
- `src/client/odata.py` — `IfsODataClient`. Owns URL building (`{base}/{service}.svc/{EntitySet}` +
  query options), the paging loop (`@odata.nextLink` → `$skip` fallback → 100k cap), retry/backoff,
  meta-field stripping, and streaming rows out (generator; never buffers a whole extract in memory).
- `src/client/metadata.py` — EDMX `$metadata` parser (stdlib `xml.etree.ElementTree`; `lxml` only if
  namespace handling forces it): entity sets, properties + `Edm.*` types, keys, nav-props. Powers both
  the type mapping and the discovery sync actions.
- `src/configuration.py` — Pydantic models: `Configuration` (root/connection), a nested
  `AdvancedConfig` sub-model (the collapsible Advanced section of §5 — `base_path`, `page_size`,
  `request_timeout`, `max_retries`, exposed as `Configuration.advanced` and read as `config.advanced.*`),
  and `RowConfiguration` (per-table). Typed fields (enums for `fetch_type`/`load_type`; no raw
  `dict`/`Any`); `#client_secret` via `Field(alias="#client_secret")`; `extra="ignore"` set explicitly;
  validators tolerate empty/None; a `computed_field` `incremental` derived from `load_type`. No `debug`
  field (the base consumes the platform `debug`). Partial instantiation only where a sync action needs
  fewer fields than `run()`.
- `src/component.py` — `Component(ComponentBase)`. Clients built in `__init__` (config permitting);
  `run()` under ~30 lines delegating to private methods: `_get_config`, `_discover_schema`
  (EDMX → columns/types/PK), `_build_filter` (watermark `gt` + user `$filter`), `_fetch` (paged
  generator), `_write` (stream to CSV + build `schema` manifest), `_advance_state`. `@sync_action`
  methods use `self.client` (no client init inside them).

**Native types / manifest:** emit the **authoritative `schema`** manifest (`data_type.base.type`) —
`dataTypeSupport = authoritative` is set in the Developer Portal in Phase 6 (CF default for new
components), and the code writes the `schema` format to match. If a header row is written for
debuggability, pass `has_header=True` so Storage skips it (a `schema` manifest defaults to
`has_header: false`). PK from the chosen key columns; `incremental` from `load_type`.

**Error handling** — `UserException` (exit 1) for user-fixable conditions: token/auth failure (bad
`client_id`/secret/realm/tenant), `403` (missing permission set or no Allowed Company), `400`
(malformed `$filter`), unknown service/entity set, missing required row fields (PK/incremental_field).
Unexpected failures bubble up as exit 2. Sync actions never raise on a missing-config precondition
(return a guidance item); they raise `UserException` on a real API/HTTP error.

**Scratch files:** any temp work goes to `/tmp`, never `data/out/tables/` (everything there is
uploaded to Storage).

**Key dependencies:** `keboola.component` (Common Interface, manifests, state), `pydantic` (config
models), `requests` (HTTP), stdlib `xml.etree.ElementTree` (EDMX). Deliberately no heavyweight OData
SDK — mirrors the SAP OData sibling and keeps the image lean. Deferring a shared OData engine with the
SAP sibling (design O3) — YAGNI now.

## 7. Testing

- **Datadir tests** (`tests/functional/*`, `keboola.datadirtest`): each case is a single merged
  `config.json` (root+row shape) + a row-scoped `state.json`. Cases:
  - happy path — `VoucherRowsAnalysis` / `VoucherRowSet`, full fetch + full load → expected CSV +
    `schema` manifest (a real produced row inspected, not just the manifest).
  - **Test-env requirement for native types:** the datadir/VCR harness must run with
    `KBC_DATA_TYPE_SUPPORT=authoritative` set, otherwise `create_out_table_definition` auto-detects
    legacy mode and silently emits `columns`+`column_metadata` instead of the `schema` format — the
    fixtures would then validate against the wrong manifest shape. Set it in the test setup (e.g.
    `docker-compose`/`build_n_test.sh` env) so the expected `schema` manifests are actually produced.
  - incremental — seeded `state.json` watermark → asserts the `gt` `$filter` is applied and state
    advances; PK-based upsert (`incremental=True`).
  - paging — a multi-page `@odata.nextLink` sequence collapses into one table.
  - meta-field stripping — `@odata.etag`/`luname`/`keyref`/`Objgrants` absent from output columns.
  - error cases → exit 1: auth failure, `403` (no permission / no Allowed Company), `400` bad
    `$filter`, unknown service/entity set.
- **VCR strategy** (`generate-vcr-tests`): record the token exchange + the example projection reads —
  `VoucherRowsAnalysis.svc/$metadata`, `VoucherRowSet` (one or more pages), and `AllProjections`
  (`VoucherRowsAnalysis` is used only because its OpenAPI is already on disk — no domain significance).
  Sanitizers redact the `Authorization: Bearer` header, `client_secret`
  and token bodies, and rewrite the tenant host / realm to placeholders (`{host}` / `{realm}`) in URIs
  and payloads, so no tenant identifier lands in a committed cassette. Recording requires live IFS
  credentials → **Phase 5 is blocked on provisioning (O1)**.
- **Sync-action tests:** `testConnection` (success + failure→`UserException`), `validate_query`
  (valid query → ok; malformed `$filter` → OData `error.message` surfaced), `list_services` (catalog +
  free-text fallback + missing-config guidance item), `list_entitysets` / `list_columns` /
  `list_primary_keys` / `list_incremental_fields` parsed from a recorded `$metadata` fixture.
- **Seed payloads already on disk:** `VoucherRowsAnalysis.openapi.json` (the real service's OpenAPI)
  seeds column/entity-set expectations for the metadata-parser unit tests immediately (no creds
  needed); the live `VoucherRowSet` JSON is captured during provisioning as the first cassette.

## 8. Deployment & validation (CF test project)

- **kbagent** registers/updates the component and creates a config in the **cf-dev** project; the
  config's `runtime.tag` is overridden to the freshly-built `initial-implementation` image tag (use
  the newest CI/dev tag — a pinned tag can be stale).
- **Successful end-to-end run:** a `VoucherRowsAnalysis` / `VoucherRowSet` row for a granted company
  produces one output table with the expected columns and a non-trivial row count (the reference tenant
  returned a few hundred rows for one allowed company), job status `success`,
  resolved tag matching the branch build. An incremental second run pulls only new rows and advances
  state.
- Developer Portal value setup is Phase 6, done after the bootstrap release so CI property-sync does
  not overwrite it. That Phase 6 checklist must include, at minimum: the config + row schemas, every
  `@sync_action` **registered in the portal** (an action in the image but not the portal 404s
  on-platform), `dataTypeSupport = authoritative` (else the `schema` manifest is silently downgraded
  to legacy hints — §6), and **`default_bucket: true`** (this is what actually activates the
  `in.c-keboola.ex-ifs-{configId}` routing the component relies on in §2; without it the output tables
  have no resolved destination). Confirm each with a fresh `kbagent dev-portal` GET.

## 9. Open risks & blockers

1. **Provisioning (O1) — the one real gate.** An IFS admin must create the confidential IAM Client
   (Service Accounts), auto-create the service user via the Username field, grant `IFSREADONLYSUPPORT`,
   and add at least one Allowed Company. Without it, cassette recording (Phase 5) and the cf-dev smoke
   test (Phase 7) cannot run. Design/spec work is not blocked. Owner: customer/IFS admin.
2. **Token TTL (~180 s) refresh.** A long extraction outlives one token; the client must refresh
   mid-run (explicit requirement, §3). Mitigated by design; verify under a large real extract in
   Phase 7.
3. **Paging runtime shape.** `@odata.nextLink` / `$skiptoken` behaviour and the server page cap are
   OData-standard but not in the OpenAPI; confirm on the first real `VoucherRowSet?$top=100`. The
   nextLink-then-`$skip`-fallback strategy is safe either way.
4. **`AllProjections` API-class field.** The exact column encoding Standard-vs-custom is not
   confirmable without a live `AllProjections.svc/$metadata` call; `list_services` still works
   (unfiltered list + free-text), so this only refines the Standard filter. Owner: maintainer, one
   authenticated call.
5. **Company-scoped RLS.** Data is empty until companies are granted to the service user; a
   correctly-authenticated run can still return zero rows if no company is allowed — surface this
   clearly (empty result is not an error, but log the allowed-company caveat). Owner: customer.
6. **Custom/QuickReport schema instability (D2).** Custom services can change or vanish; supported
   on-demand via free-text + live `$metadata`, not defaulted — accepted risk.
```
