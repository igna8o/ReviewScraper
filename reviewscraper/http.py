"""One shared HTTP client for requests to Google."""

from __future__ import annotations

import asyncio
import os

import httpx

DEFAULT_BASE_URL = "https://www.google.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 64 << 20


class GoogleHTTPError(RuntimeError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status


class GoogleHTTP:
    """GET with timeouts, a size cap and a small retry on transient errors.

    ``base_url`` defaults to ``REVIEWSCRAPER_GOOGLE_BASE_URL`` or
    https://www.google.com; ``proxy`` to ``REVIEWSCRAPER_PROXY``.
    """

    def __init__(self, base_url: str | None = None, proxy: str | None = None,
                 timeout: float = 30.0, retries: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or os.environ.get("REVIEWSCRAPER_GOOGLE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.retries = retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy or os.environ.get("REVIEWSCRAPER_PROXY") or None,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, str]) -> str:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            try:
                response = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                last_exc = GoogleHTTPError(response.status_code, url)
                continue
            if response.status_code != 200:
                raise GoogleHTTPError(response.status_code, url)
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"response from {url} is larger than {MAX_RESPONSE_BYTES} bytes")
            return response.text
        assert last_exc is not None
        raise last_exc
