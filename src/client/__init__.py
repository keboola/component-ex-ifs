"""HTTP client layer for the IFS Cloud OData extractor.

Separated from ``component.py``: :mod:`client.auth` owns the Keycloak
``client_credentials`` token exchange, :mod:`client.metadata` parses EDMX
``$metadata``, and :mod:`client.odata` owns URL building, paging, retry and
meta-field stripping.
"""
