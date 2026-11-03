"""Google Maps place search and reviews over Google's own web endpoints.

Neither endpoint is a public API. Both are what Google's own pages request,
replayed over plain HTTP without cookies:

* Search: ``/search?tbm=map`` with a ``pb`` descriptor, the request the Maps
  page sends for its result list. Each result holds its place record at
  ``[1]``, with the feature ID (``0x...:0x...``) at ``[1][10]`` and the name at
  ``[1][11]``.
* Reviews: ``/httpservice/web/PrivateLocalSearchUiDataService/GetLocalBoqProxy``,
  the request the "all Google reviews" window on Search results sends for each
  page of 10 reviews. ``reqpld`` carries the feature ID, the sort order and the
  previous page's token.

The request shapes and response positions follow the measurements published by
HazelnutParadise/insyra (MIT, 2026-09-13), which found that the older
``/maps/rpc/listugcposts`` route answers 403 when signed out. Google can change
any of this without notice; when a response no longer has the expected shape,
the parser raises ``FormatChanged`` instead of returning an empty result.
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable


from ..http import GoogleHTTP

SEARCH_PATH = "/search"
REVIEWS_PATH = "/httpservice/web/PrivateLocalSearchUiDataService/GetLocalBoqProxy"

# Cut-down search descriptor: 7i20 asks for 20 places; without 10b1 or 34m19
# Google returns no place records.
SEARCH_PB = (
    "!7i20!10b1!34m19!2b1!3b1!4b1!6b1!8m6!1b1!3b1!4b1!5b1!6b1!7b1!9b1"
    "!12b1!14b1!20b1!23b1!25b1!26b1!31b1"
)
SEARCH_RESULTS_INDEX = 64
REVIEWS_PER_PAGE = 10

SORTS = {
    "most_relevant": 1,
    "newest": 2,
    "highest_rating": 3,
    "lowest_rating": 4,
}

FEATURE_ID_RE = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+", re.IGNORECASE)
CONTRIB_ID_RE = re.compile(r"/contrib/(\d+)")


class FormatChanged(RuntimeError):
    """Google answered, but not in the shape this module knows how to read."""


@dataclass
class Place:
    google_id: str
    name: str = ""
    query: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "name": self.name,
            "google_id": self.google_id,
            "location_link": f"https://www.google.com/maps/place/data=!4m2!3m1!1s{self.google_id}",
        }


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def strip_xssi(body: str) -> str:
    """Remove the ``)]}'`` line Google puts in front of its JSON."""
    body = body.lstrip()
    if body.startswith(")]}'"):
        body = body[4:]
    return body


def dig(data: Any, *path: int) -> Any:
    """Follow list indices, returning None when any step is missing."""
    current = data
    for idx in path:
        if not isinstance(current, list):
            return None
        if idx < 0:
            idx += len(current)
        if idx < 0 or idx >= len(current):
            return None
        current = current[idx]
    return current


def _str(data: Any, *path: int) -> str:
    value = dig(data, *path)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    return ""


def _int(data: Any, *path: int) -> int | None:
    value = dig(data, *path)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def parse_search(body: str) -> list[Place]:
    """Read places from a ``tbm=map`` search response, in Google's order."""
    try:
        data = json.loads(strip_xssi(body))
    except json.JSONDecodeError as exc:
        raise FormatChanged(f"search response is not JSON: {exc}") from exc
    results = dig(data, SEARCH_RESULTS_INDEX)
    if results is None:
        raise FormatChanged("search response has no result list")
    places: list[Place] = []
    seen: set[str] = set()
    for entry in results if isinstance(results, list) else []:
        fid = dig(entry, 1, 10)
        if not isinstance(fid, str) or not FEATURE_ID_RE.fullmatch(fid) or fid in seen:
            continue
        seen.add(fid)
        places.append(Place(google_id=fid, name=_str(entry, 1, 11)))
    return places


def parse_review_page(body: str) -> tuple[list[dict[str, Any]], str]:
    """Read one review page; returns (reviews, next_page_token)."""
    try:
        data = json.loads(strip_xssi(body))
    except json.JSONDecodeError as exc:
        raise FormatChanged(f"review page is not JSON: {exc}") from exc
    block = dig(data, 1, 10)
    if not isinstance(block, list):
        raise FormatChanged("review page has no review block")

    next_token = dig(block, 6)
    next_token = next_token if isinstance(next_token, str) else ""
    reviews = []
    for record in dig(block, 2) or []:
        if isinstance(record, list):
            reviews.append(_review_from_record(record, next_token))
    return reviews, next_token


def _review_from_record(record: list, page_token: str) -> dict[str, Any]:
    author_link = _str(record, 3, 2)
    contrib = CONTRIB_ID_RE.search(author_link)
    millis = _int(record, 2, 2)
    timestamp = millis // 1000 if millis else None
    text = _str(record, 27)
    text = html.unescape(text.replace("<br>", "\n"))
    return {
        "review_id": _str(record, 5),
        "author_title": _str(record, 3, 0),
        "author_id": contrib.group(1) if contrib else "",
        "author_link": author_link,
        "author_reviews_count": _int(record, 3, 3),
        "author_photos_count": _int(record, 3, 4),
        "review_text": text,
        "review_rating": _int(record, 1),
        "review_timestamp": timestamp,
        "review_datetime_utc": (
            datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%m/%d/%Y %H:%M:%S")
            if timestamp
            else None
        ),
        "review_relative_time": _str(record, 2, 0),
        "review_language": _str(record, 26),
        # Token of the page after this one: pass it as last_pagination_id to
        # continue after this review's page.
        "review_pagination_id": page_token,
    }


def resolve_feature_id(query: str) -> str | None:
    """Return the feature ID if the query is one, or a Maps URL containing one."""
    match = FEATURE_ID_RE.search(query)
    return match.group(0).lower() if match else None


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #

def review_params(feature_id: str, sort: int, token: str, language: str) -> dict[str, str]:
    args: list[Any] = [None] * 24
    args[1] = sort
    args[9] = REVIEWS_PER_PAGE
    args[11] = [feature_id]
    args[16] = [1, 1, None, [[3], [4], [5], [6], [7]]]
    args[19] = token
    args[23] = 0
    request: list[Any] = [None] * 10
    request[9] = args
    return {
        "hl": language,
        "reqpld": json.dumps([None, request], separators=(",", ":")),
        "msc": "gwsrpc",
        "opi": "89978449",
    }


class GoogleMaps:
    """Search Google Maps places and read their reviews."""

    def __init__(self, http: GoogleHTTP | None = None,
                 min_delay: float = 1.0, max_delay: float = 3.0):
        self.http = http or GoogleHTTP()
        self.min_delay = min_delay
        self.max_delay = max(max_delay, min_delay)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def search(self, query: str, language: str = "en",
                     region: str | None = None, limit: int = 20) -> list[Place]:
        params = {"tbm": "map", "hl": language, "q": query, "pb": SEARCH_PB}
        if region:
            params["gl"] = region.lower()
        body = await self.http.get(SEARCH_PATH, params)
        places = parse_search(body)
        for place in places:
            place.query = query
        return places[:limit] if limit else places

    async def resolve(self, query: str, language: str = "en",
                      region: str | None = None, limit: int = 1) -> list[Place]:
        fid = resolve_feature_id(query)
        if fid:
            return [Place(google_id=fid, query=query)]
        return await self.search(query, language=language, region=region, limit=limit)

    async def iter_review_pages(self, feature_id: str, sort: str = "most_relevant",
                                language: str = "en", token: str = "",
                                ) -> AsyncIterator[tuple[list[dict[str, Any]], str]]:
        sort_code = SORTS.get(sort)
        if sort_code is None:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}")
        first = True
        while True:
            if not first:
                await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
            first = False
            body = await self.http.get(REVIEWS_PATH, review_params(feature_id, sort_code, token, language))
            reviews, token = parse_review_page(body)
            yield reviews, token
            if not token:
                return

    async def reviews(self, feature_id: str, *, reviews_limit: int = 10,
                      sort: str = "most_relevant", language: str = "en",
                      start: int | None = None, cutoff: int | None = None,
                      cutoff_rating: int | None = None, ignore_empty: bool = False,
                      reviews_query: str | None = None,
                      last_pagination_id: str | None = None) -> list[dict[str, Any]]:
        """Collect reviews for one place, applying Outscraper-style filters.

        ``reviews_limit=0`` means every review. ``start``/``cutoff`` are Unix
        timestamps (newest/oldest bound) and force ``sort='newest'``.
        ``reviews_query`` filters locally on the review text.
        """
        if start is not None or cutoff is not None:
            sort = "newest"
        if cutoff_rating is not None and sort not in ("highest_rating", "lowest_rating"):
            raise ValueError("cutoff_rating needs sort='highest_rating' or 'lowest_rating'")
        needle = reviews_query.lower() if reviews_query else None

        out: list[dict[str, Any]] = []
        async for page, _ in self.iter_review_pages(feature_id, sort, language, last_pagination_id or ""):
            stop = False
            for review in page:
                ts = review["review_timestamp"]
                rating = review["review_rating"]
                if cutoff is not None and ts is not None and ts < cutoff:
                    stop = True
                    break
                if cutoff_rating is not None and rating is not None and (
                    (sort == "highest_rating" and rating < cutoff_rating)
                    or (sort == "lowest_rating" and rating > cutoff_rating)
                ):
                    stop = True
                    break
                if start is not None and ts is not None and ts > start:
                    continue
                if ignore_empty and not review["review_text"]:
                    continue
                if needle and needle not in review["review_text"].lower():
                    continue
                out.append(review)
                if reviews_limit and len(out) >= reviews_limit:
                    return out
            if stop:
                break
        return out

    async def place_reviews(self, queries: Iterable[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Outscraper ``/maps/reviews-v3`` semantics: places with ``reviews_data``."""
        limit = kwargs.pop("limit", 1)
        language = kwargs.get("language", "en")
        region = kwargs.pop("region", None)
        results = []
        for query in queries:
            for place in await self.resolve(query, language=language, region=region, limit=limit):
                item = place.as_dict()
                item["reviews_data"] = await self.reviews(place.google_id, **kwargs)
                item["reviews_scraped"] = len(item["reviews_data"])
                results.append(item)
        return results
