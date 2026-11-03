"""Python client for a ReviewScraper server, mirroring Outscraper's ``ApiClient``.

    from reviewscraper.client import ApiClient
    client = ApiClient(api_key="...", base_url="http://localhost:8000")
    places = client.google_maps_reviews("Din Tai Fung, Taipei", reviews_limit=50, sort="newest")

Method names and keyword arguments match the Outscraper SDK for the endpoints
this server implements, so switching usually means changing the import and
passing ``base_url``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class ApiClient:
    def __init__(self, api_key: str | None = None, base_url: str = "http://localhost:8000",
                 timeout: float = 600, poll_interval: float = 3, max_wait: float = 3600):
        headers = {"X-API-KEY": api_key} if api_key else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing --------------------------------------------------------- #

    def _get(self, path: str, params: dict[str, Any], async_request: bool) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        params["async"] = "true" if async_request else "false"
        response = self._http.get(path, params=params)
        response.raise_for_status()
        body = response.json()
        return body if async_request else body.get("data", [])

    def get_request_archive(self, request_id: str) -> dict[str, Any]:
        response = self._http.get(f"/requests/{request_id}")
        response.raise_for_status()
        return response.json()

    def wait_request_archive(self, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            record = self.get_request_archive(request_id)
            if record["status"] != "Pending":
                return record
            time.sleep(self.poll_interval)
        raise TimeoutError(f"request {request_id} still pending after {self.max_wait}s")

    def get_requests_history(self, type: str = "running", skip: int = 0, page_size: int = 25) -> list:
        response = self._http.get("/requests", params={"type": type, "skip": skip, "pageSize": page_size})
        response.raise_for_status()
        return response.json()

    # -- endpoints -------------------------------------------------------- #

    def google_maps_reviews(self, query: str | list[str], reviews_limit: int = 10, limit: int = 1,
                            sort: str = "most_relevant", start: int | None = None,
                            cutoff: int | None = None, cutoff_rating: int | None = None,
                            ignore_empty: bool = False, language: str = "en",
                            region: str | None = None, reviews_query: str | None = None,
                            last_pagination_id: str | None = None,
                            fields: str | list[str] | None = None,
                            async_request: bool = False, webhook: str | None = None) -> Any:
        params = {
            "query": [query] if isinstance(query, str) else list(query),
            "reviewsLimit": reviews_limit,
            "limit": limit,
            "sort": sort,
            "start": start,
            "cutoff": cutoff,
            "cutoffRating": cutoff_rating,
            "ignoreEmpty": str(ignore_empty).lower(),
            "language": language,
            "region": region,
            "reviewsQuery": reviews_query,
            "lastPaginationId": last_pagination_id,
            "fields": ",".join(fields) if isinstance(fields, list) else fields,
            "webhook": webhook,
        }
        return self._run("/maps/reviews-v3", params, async_request)

    def google_maps_search(self, query: str | list[str], limit: int = 20, language: str = "en",
                           region: str | None = None, fields: str | list[str] | None = None,
                           async_request: bool = False, webhook: str | None = None) -> Any:
        params = {
            "query": [query] if isinstance(query, str) else list(query),
            "limit": limit,
            "language": language,
            "region": region,
            "fields": ",".join(fields) if isinstance(fields, list) else fields,
            "webhook": webhook,
        }
        return self._run("/maps/search-v3", params, async_request)

    def _run(self, path: str, params: dict[str, Any], async_request: bool) -> Any:
        if async_request:
            return self._get(path, params, async_request=True)
        # Big jobs run in the background on the server; wait for them here.
        submitted = self._get(path, params, async_request=True)
        record = self.wait_request_archive(submitted["id"])
        if record["status"] == "Error":
            raise RuntimeError(record.get("errorMessage", "request failed"))
        return record.get("data", [])
