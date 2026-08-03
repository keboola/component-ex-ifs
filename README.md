# IFS Cloud Extractor (`keboola.ex-ifs`)

A single, generic, config-driven Keboola extractor for **IFS Cloud OData v4 projection services**. Point it at an IFS Cloud tenant and extract any projection service / entity set as a Keboola table — one component, N configuration rows, no per-service code.

**Table of Contents:**

[TOC]

Functionality Notes
===================

IFS Cloud exposes thousands of OData v4 projection services. This component connects to a tenant and, per configuration row, extracts one projection entity set into one Keboola table. It discovers services, entity sets, columns, and keys live from the tenant's OData metadata (EDMX `$metadata`), maps OData EDM types to Keboola native (authoritative) data types, follows server-driven paging, and supports full or incremental loads.

Prerequisites
=============

Access is provisioned **inside IFS** by an administrator — none of this is component configuration:

1. In **IFS Solution Manager → Users and Permissions → Identity and Access Manager → IAM Client**, create a confidential IAM Client: set a Client ID, uncheck **Public Client** (yields a Client Secret), and enable **Service Accounts** (turns on the `client_credentials` grant).
2. Create the IFS Service User via the IAM Client's **Username** field (on save, IFS creates the service user with the correct directory identity).
3. Grant the service user a read permission set — `IFSREADONLYSUPPORT` ("Read-Only Grants for all Projections") is sufficient for projection reads.
4. Add the companies you intend to pull to the service user's **Allowed Companies** (data is company-scoped by row-level security; results are empty until at least one company is granted).

Hand the component operator the Keycloak **realm**, the **Client ID**, the **Client Secret**, and the granted **company codes**.

Features
========

| **Feature**              | **Description**                                                        |
|--------------------------|------------------------------------------------------------------------|
| Generic, config-driven   | Any projection service / entity set via configuration — no per-service code. |
| Row-based configuration  | One configuration row → one output table.                              |
| OAuth 2.0 (`client_credentials`) | Machine-to-machine auth against the tenant's Keycloak, with mid-run token refresh. |
| Live schema discovery    | Services, entity sets, columns, keys, and incremental fields via sync actions. |
| Native data types        | OData EDM → authoritative Keboola `schema` (decimal precision clamped to backend limits). |
| Incremental loading       | Per-row `gt` watermark on a chosen column, stored in state and advanced after a successful write. |
| Server-driven paging     | Follows `@odata.nextLink`, with a `$skip` fallback; streams rows (no full buffering). |

Configuration
=============

**Connection (root configuration)**

| Parameter | Description |
|---|---|
| `tenant_id` | IFS Cloud tenant; the host resolves to `{tenant_id}.ifs.cloud`. |
| `realm` | Keycloak realm. |
| `client_id` | Confidential IAM Client id. |
| `#client_secret` | IAM Client secret (encrypted). |
| `service_account` | Optional — the IFS service-user identity, for operator reference. |
| *Advanced* | Optional tuning: base path, page size, request timeout, max retries. |

**Table (configuration row)**

| Parameter | Description |
|---|---|
| `service` | Projection service (pick from the live catalog or type a custom service name). |
| `entity_set` | Entity set within the service. |
| `columns` | Optional `$select` — subset of columns (empty = all). |
| `primary_key` | Primary key columns (required for incremental load). |
| `fetch_type` | `full_fetch` or `incremental_fetch` (server-side `$filter` watermark). |
| `incremental_field` | Cursor column (required for `incremental_fetch`). |
| `load_type` | `full_load` or `incremental_load` (Keboola Storage write mode). |
| `filter` | Optional additional OData `$filter`. |
| `order_by` | Optional OData `$orderby`. |
| `keep_meta_fields` | Keep the IFS meta-fields (`@odata.etag`, `luname`, `keyref`, `Objgrants`); off by default. |

Sync actions (available in the UI): **Test Connection**, **Validate Query**, and dropdown loaders for services, entity sets, columns, primary keys, and incremental fields.

Output
======

One table per configuration row, named after the entity set, with an authoritative `schema` manifest (native base types and primary key). Incremental rows upsert on the primary key; the watermark is persisted to component state and advanced only after a successful write.

Development
-----------

Clone the repository, then build and run with Docker Compose:

~~~~
git clone https://github.com/keboola/component-ex-ifs
cd component-ex-ifs
docker-compose build
docker-compose run --rm dev
~~~~

Run the test suite and lint checks:

~~~~
docker-compose run --rm test
~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer documentation](https://developers.keboola.com/extend/component/deployment/).
