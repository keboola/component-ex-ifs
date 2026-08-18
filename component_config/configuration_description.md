Set up the connection once in the root configuration — the IFS Cloud tenant, the Keycloak realm, and the OAuth client credentials — then add one row per table you want to extract.

Each row selects a projection service and entity set (pick from the live catalog or type a custom service name), and optionally a destination table name, a column subset, an OData `$filter`, ordering, a primary key, and a date window (a date field bounded by Date Start and Date End) to limit the fetch. Set the table name to pull the same entity set into more than one table.

The IFS service account must be provisioned inside IFS beforehand: an IAM Client with the `client_credentials` grant enabled, a read permission set, and the companies you intend to pull granted as Allowed Companies. See the documentation for the setup steps.
