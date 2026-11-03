"""HTTP API with the same request and response shape as Outscraper's.

Run it with ``reviewscraper serve`` (or ``uvicorn reviewscraper.api:app``).

Endpoints:

* ``GET /maps/reviews-v3`` - places with their reviews (``reviews_data``)
* ``GET /maps/search-v3``  - places for a text query
* ``GET /places/autocomplete`` - business suggestions while typing (official Places API)
* ``GET /places/details``  - one suggestion resolved to a place with its ``google_id``
* ``GET /requests/{id}``   - result of an async request
* ``GET /requests``        - running or finished requests
* ``GET /health``

Authentication uses the ``X-API-KEY`` header against the comma-separated keys
in ``REVIEWSCRAPER_API_KEYS``. With no keys configured, authentication is off,
which is only sensible when the server listens on localhost.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from .jobs import JobStore
from .providers.google_maps import SORTS, GoogleMaps
from .providers.places import PlacesAPI, PlacesAPIError

log = logging.getLogger("reviewscraper.api")


def _api_keys() -> set[str]:
    raw = os.environ.get("REVIEWSCRAPER_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def _filter_fields(items: list[dict[str, Any]], fields: str | None) -> list[dict[str, Any]]:
    if not fields:
        return items
    wanted = {f.strip() for f in fields.split(",") if f.strip()}
    out = []
    for item in items:
        place = {k: v for k, v in item.items() if k in wanted or k == "reviews_data"}
        if "reviews_data" in place:
            review_keys = wanted & set().union(*(r.keys() for r in place["reviews_data"])) if place["reviews_data"] else set()
            if review_keys:
                place["reviews_data"] = [{k: v for k, v in r.items() if k in review_keys} for r in place["reviews_data"]]
            elif "reviews_data" not in wanted:
                place.pop("reviews_data")
        out.append(place)
    return out


def create_app(maps: GoogleMaps | None = None, db_path: str | None = None,
               jobs: JobStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.maps = maps or GoogleMaps(
            min_delay=float(os.environ.get("REVIEWSCRAPER_MIN_DELAY", "1")),
            max_delay=float(os.environ.get("REVIEWSCRAPER_MAX_DELAY", "3")),
            places=PlacesAPI.from_env(),
        )
        app.state.jobs = jobs or JobStore(
            path=db_path or os.environ.get("REVIEWSCRAPER_DB", "reviewscraper.db"),
            ttl=float(os.environ.get("REVIEWSCRAPER_RESULT_TTL", str(2 * 3600))),
            max_concurrency=int(os.environ.get("REVIEWSCRAPER_CONCURRENCY", "4")),
        )
        if not _api_keys():
            log.warning("REVIEWSCRAPER_API_KEYS is not set: the API accepts requests without a key")
        yield
        await app.state.jobs.drain()
        await app.state.maps.aclose()
        app.state.jobs.close()

    app = FastAPI(title="ReviewScraper", version="0.1.0", lifespan=lifespan)

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        keys = _api_keys()
        if keys and x_api_key not in keys:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-KEY")

    async def dispatch(request: Request, endpoint: str, queries: list[str],
                       work: Callable[[], Awaitable[Any]], is_async: bool,
                       webhook: str | None) -> dict[str, Any]:
        jobs: JobStore = request.app.state.jobs
        request_id = jobs.create(endpoint, queries, webhook)
        location = str(request.url_for("get_request", request_id=request_id))
        if is_async:
            jobs.submit(request_id, work, webhook, location)
            return {"id": request_id, "status": "Pending", "results_location": location}
        await jobs.run(request_id, work)
        record = jobs.get(request_id) or {}
        if record.get("status") == "Error":
            raise HTTPException(status_code=502, detail={"id": request_id, "error": True,
                                                         "errorMessage": record.get("errorMessage")})
        return {"id": request_id, "status": record["status"], "data": record.get("data", [])}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/maps/reviews-v3", dependencies=[Depends(require_key)])
    async def maps_reviews(
        request: Request,
        query: list[str] = Query(..., description="Place name, search, feature ID (0x..:0x..) or Maps URL; repeat for several"),
        reviewsLimit: int = Query(10, ge=0, description="Reviews per place; 0 = all"),
        limit: int = Query(1, ge=1, le=20, description="Places per search query"),
        sort: str = Query("most_relevant", description=", ".join(SORTS)),
        start: int | None = None,
        cutoff: int | None = None,
        cutoffRating: int | None = Query(None, ge=1, le=5),
        ignoreEmpty: bool = False,
        reviewsQuery: str | None = None,
        lastPaginationId: str | None = None,
        language: str = "en",
        region: str | None = None,
        fields: str | None = None,
        is_async: bool = Query(True, alias="async"),
        webhook: str | None = None,
    ) -> dict[str, Any]:
        if sort not in SORTS:
            raise HTTPException(status_code=422, detail=f"sort must be one of {', '.join(SORTS)}")
        if cutoffRating is not None and sort not in ("highest_rating", "lowest_rating"):
            raise HTTPException(status_code=422, detail="cutoffRating needs sort=highest_rating or lowest_rating")
        maps: GoogleMaps = request.app.state.maps

        async def work() -> list[dict[str, Any]]:
            places = await maps.place_reviews(
                query, limit=limit, region=region, reviews_limit=reviewsLimit, sort=sort,
                language=language, start=start, cutoff=cutoff, cutoff_rating=cutoffRating,
                ignore_empty=ignoreEmpty, reviews_query=reviewsQuery,
                last_pagination_id=lastPaginationId,
            )
            return _filter_fields(places, fields)

        return await dispatch(request, "maps/reviews-v3", query, work, is_async, webhook)

    @app.get("/maps/search-v3", dependencies=[Depends(require_key)])
    async def maps_search(
        request: Request,
        query: list[str] = Query(...),
        limit: int = Query(20, ge=1, le=20),
        language: str = "en",
        region: str | None = None,
        fields: str | None = None,
        is_async: bool = Query(True, alias="async"),
        webhook: str | None = None,
    ) -> dict[str, Any]:
        maps: GoogleMaps = request.app.state.maps

        async def work() -> list[list[dict[str, Any]]]:
            out = []
            for q in query:
                places = await maps.search(q, language=language, region=region, limit=limit)
                out.append(_filter_fields([p.as_dict() for p in places], fields))
            return out

        return await dispatch(request, "maps/search-v3", query, work, is_async, webhook)

    def places_api(request: Request) -> PlacesAPI:
        places = request.app.state.maps.places
        if places is None:
            raise HTTPException(status_code=501, detail="GOOGLE_PLACES_API_KEY is not configured")
        return places

    @app.get("/places/autocomplete", dependencies=[Depends(require_key)])
    async def places_autocomplete(request: Request, input: str = Query(..., min_length=1),
                                  language: str = "en", region: str | None = None,
                                  sessionToken: str | None = None) -> dict[str, Any]:
        try:
            suggestions = await places_api(request).autocomplete(
                input, language=language, region=region, session_token=sessionToken)
        except PlacesAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"data": suggestions}

    @app.get("/places/details", dependencies=[Depends(require_key)])
    async def places_details(request: Request, placeId: str, language: str = "en",
                             sessionToken: str | None = None) -> dict[str, Any]:
        try:
            place = await places_api(request).details(placeId, language=language, session_token=sessionToken)
        except PlacesAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"data": place.as_dict()}

    @app.get("/requests/{request_id}", name="get_request", dependencies=[Depends(require_key)])
    async def get_request(request: Request, request_id: str) -> dict[str, Any]:
        record = request.app.state.jobs.get(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="request not found or expired")
        return record

    @app.get("/requests", dependencies=[Depends(require_key)])
    async def list_requests(request: Request, type: str = Query("running", pattern="^(running|finished)$"),
                            skip: int = Query(0, ge=0), pageSize: int = Query(25, ge=1, le=100)) -> list[dict[str, Any]]:
        return request.app.state.jobs.list(type, skip, pageSize)

    return app


app = create_app()
