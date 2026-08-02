"""IFS Cloud OData v4 client.

Owns URL building, the paging loop (``@odata.nextLink`` -> ``$skip`` fallback ->
hard cap), retry/backoff, meta-field stripping, and streaming rows out as a
generator (never buffering a whole extract in memory).
"""

import time
from collections.abc import Iterator
from urllib.parse import quote, urljoin

import requests
from keboola.component.exceptions import UserException

from client.auth import IfsAuthClient

# IFS meta-fields that ride along on data rows but are not real data columns.
_META_FIELDS = frozenset({"@odata.etag", "luname", "keyref", "Objgrants"})
_MAX_PAGES = 100_000
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 60.0


class IfsODataClient:
    """Reads OData collections and per-service metadata from IFS Cloud."""

    def __init__(
        self,
        host: str,
        base_path: str,
        auth: IfsAuthClient,
        session: requests.Session | None = None,
        page_size: int = 1000,
        timeout: int = 60,
        max_retries: int = 5,
    ) -> None:
        self._host = host
        self._base_path = base_path.rstrip("/")
        self._auth = auth
        self._session = session or requests.Session()
        self._page_size = page_size
        self._timeout = timeout
        self._max_retries = max_retries

    # --- URL building --------------------------------------------------
    @property
    def _root(self) -> str:
        return f"https://{self._host}{self._base_path}"

    def service_url(self, service: str) -> str:
        return f"{self._root}/{service}.svc"

    def _collection_url(
        self,
        service: str,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter: str | None = None,
        orderby: str | None = None,
        expand: str | None = None,
        top: int | None = None,
        skip: int | None = None,
    ) -> str:
        options: dict[str, str] = {}
        if select:
            options["$select"] = ",".join(select)
        if filter:
            options["$filter"] = filter
        if orderby:
            options["$orderby"] = orderby
        if expand:
            options["$expand"] = expand
        if top is not None:
            options["$top"] = str(top)
        if skip is not None:
            options["$skip"] = str(skip)
        base = f"{self.service_url(service)}/{entity_set}"
        if not options:
            return base
        query = "&".join(f"{key}={quote(value, safe=',')}" for key, value in options.items())
        return f"{base}?{query}"

    # --- public reads --------------------------------------------------
    def get_metadata(self, service: str) -> str:
        """Return the raw EDMX ``$metadata`` document for a service."""
        resp = self._request(f"{self.service_url(service)}/$metadata")
        return resp.text

    def iter_rows(
        self,
        service: str,
        entity_set: str,
        *,
        select: list[str] | None = None,
        filter: str | None = None,
        orderby: str | None = None,
        expand: str | None = None,
        strip_meta: bool = True,
    ) -> Iterator[dict]:
        """Stream all rows of an entity set, following server paging.

        Follows ``@odata.nextLink`` verbatim; if a page arrives full
        (``== page_size``) with no nextLink, falls back to client-driven
        ``$skip`` until a short/empty page. A hard page cap guards against an
        infinite loop.
        """
        url: str | None = self._collection_url(
            service,
            entity_set,
            select=select,
            filter=filter,
            orderby=orderby,
            expand=expand,
            top=self._page_size,
        )
        skip = 0
        pages = 0
        while url is not None:
            pages += 1
            if pages > _MAX_PAGES:
                raise UserException(f"Paging exceeded the safety cap of {_MAX_PAGES} pages for {service}/{entity_set}.")
            body = self._request(url).json()
            rows = body.get("value", [])
            for row in rows:
                yield self._strip(row) if strip_meta else row
            next_link = body.get("@odata.nextLink")
            if next_link:
                url = urljoin(f"{self._root}/", next_link)
            elif len(rows) == self._page_size:
                skip += self._page_size
                url = self._collection_url(
                    service,
                    entity_set,
                    select=select,
                    filter=filter,
                    orderby=orderby,
                    expand=expand,
                    top=self._page_size,
                    skip=skip,
                )
            else:
                url = None

    def list_projections(self) -> list[dict]:
        """Return the tenant projection catalog (``AllProjections.svc/Projections``)."""
        return list(self.iter_rows("AllProjections", "Projections"))

    # --- internals -----------------------------------------------------
    @staticmethod
    def _strip(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in _META_FIELDS and not key.startswith("@odata.")}

    def _request(self, url: str) -> requests.Response:
        auth_retried = False
        backoff_attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self._auth.get_token()}",
                "Accept": "application/json",
            }
            resp = self._session.get(url, headers=headers, timeout=self._timeout)
            code = resp.status_code
            if code == 200:
                return resp
            if code == 401 and not auth_retried:
                self._auth.invalidate()
                auth_retried = True
                continue
            if code == 429 or 500 <= code < 600:
                if backoff_attempt < self._max_retries:
                    self._sleep_backoff(resp, backoff_attempt)
                    backoff_attempt += 1
                    continue
                raise UserException(f"IFS request failed after {self._max_retries} retries ({code}): {url}")
            raise UserException(self._error_message(resp, code))

    @staticmethod
    def _sleep_backoff(resp: requests.Response, attempt: int) -> None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = min(_BACKOFF_BASE_S * (2**attempt), _BACKOFF_CAP_S)
        time.sleep(delay)

    @staticmethod
    def _error_message(resp: requests.Response, code: int) -> str:
        detail = ""
        try:
            error = resp.json().get("error", {})
            message = error.get("message", "")
            if isinstance(message, dict):
                message = message.get("value", "")
            detail = message or error.get("code", "")
        except ValueError, AttributeError:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return f"IFS request failed ({code}){suffix}"
