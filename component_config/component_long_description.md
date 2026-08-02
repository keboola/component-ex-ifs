IFS Cloud exposes thousands of OData v4 projection services. This extractor connects to any IFS Cloud tenant and pulls data from any projection service and entity set through configuration alone — no per-service code.

It discovers services and their entity sets, columns, and keys live from the tenant's OData metadata, maps OData EDM types to Keboola native (authoritative) data types, and supports incremental loading with a per-row watermark. Server-driven paging assembles large result sets into a single table.

Authentication uses the OAuth 2.0 `client_credentials` grant against the tenant's Keycloak identity provider, with automatic mid-run token refresh. Each configuration row extracts one projection entity set into one Keboola table, with optional column selection, server-side `$filter`, ordering, and full or incremental loads.
