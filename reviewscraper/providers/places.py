"""Official Google Places API (New): business search for onboarding.

Used so a customer can type their business name and pick it from a list,
without scraping Google Search (which answers cloud IPs with a CAPTCHA).
Place Details returns ``googleMapsUri`` (``https://maps.google.com/?cid=...``),
and the CID becomes the feature ID the review endpoint accepts.

Needs ``GOOGLE_PLACES_API_KEY`` with "Places API (New)" enabled in Google
Cloud. Verified live on 2026-11-04 with a real key: autocomplete and details
for the Empire State Building, and the resulting ``0x0:0x...`` feature ID
returns real reviews (``pytest -m live``).

Pass the same ``session_token`` (any UUID) to the autocomplete calls and the
final details call of one search so Google bills them as one session.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import time
from typing import Any
from urllib.parse import quote

import httpx

from .google_maps import CID_RE, Place, cid_to_feature_id

PLACES_URL = "https://places.googleapis.com/v1"
DETAILS_FIELDS = "id,displayName,formattedAddress,googleMapsUri,rating,userRatingCount"


class PlacesAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PlaceCache:
    """Details already fetched from Places, keyed by feature ID.

    Places cannot look a place up by CID or feature ID, so a review request
    that only has ``0x0:0x...`` cannot ask it for the name. Instead
    ``/places/details`` remembers what it returned and later requests for the
    same place reuse it. Entries expire after ``ttl`` (default 30 days) because
    Google limits how long Places data may be kept.
    """

    def __init__(self, path: str = ":memory:", ttl: float = 30 * 86400):
        self.ttl = ttl
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS places ("
                         "google_id TEXT PRIMARY KEY, data TEXT NOT NULL, saved_at REAL NOT NULL)")
        self._db.commit()

    def put(self, place: Place) -> None:
        self._db.execute("INSERT OR REPLACE INTO places VALUES (?, ?, ?)",
                         (place.google_id, json.dumps(dataclasses.asdict(place)), time.time()))
        self._db.commit()

    def get(self, google_id: str) -> Place | None:
        row = self._db.execute("SELECT data, saved_at FROM places WHERE google_id=?", (google_id,)).fetchone()
        if row is None:
            return None
        if time.time() - row[1] > self.ttl:
            self._db.execute("DELETE FROM places WHERE google_id=?", (google_id,))
            self._db.commit()
            return None
        return Place(**json.loads(row[0]))

    def close(self) -> None:
        self._db.close()


class PlacesAPI:
    def __init__(self, api_key: str | None = None, timeout: float = 15,
                 transport: httpx.AsyncBaseTransport | None = None,
                 cache: PlaceCache | None = None):
        self.cache = cache
        self.api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not self.api_key:
            raise ValueError("GOOGLE_PLACES_API_KEY is not set")
        self._client = httpx.AsyncClient(base_url=PLACES_URL, timeout=timeout, transport=transport,
                                         headers={"X-Goog-Api-Key": self.api_key})

    @classmethod
    def from_env(cls, cache_path: str | None = None) -> "PlacesAPI | None":
        if not os.environ.get("GOOGLE_PLACES_API_KEY"):
            return None
        return cls(cache=PlaceCache(cache_path or ":memory:"))

    async def aclose(self) -> None:
        await self._client.aclose()
        if self.cache:
            self.cache.close()

    @staticmethod
    def _check(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            try:
                detail = response.json()["error"]["message"].strip()
            except (ValueError, KeyError, TypeError, AttributeError):
                detail = response.text[:300]
            raise PlacesAPIError(f"Places API HTTP {response.status_code}: {detail}", response.status_code)
        return response.json()

    async def autocomplete(self, text: str, language: str = "en", region: str | None = None,
                           session_token: str | None = None) -> list[dict[str, str]]:
        """Suggestions for a partly typed business name: place_id, name, address."""
        body: dict[str, Any] = {"input": text, "languageCode": language}
        if region:
            body["includedRegionCodes"] = [region.lower()]
        if session_token:
            body["sessionToken"] = session_token
        data = self._check(await self._client.post("/places:autocomplete", json=body))
        out = []
        for suggestion in data.get("suggestions", []):
            pred = suggestion.get("placePrediction")
            if not pred:
                continue
            fmt = pred.get("structuredFormat", {})
            out.append({
                "place_id": pred.get("placeId", ""),
                "name": fmt.get("mainText", {}).get("text") or pred.get("text", {}).get("text", ""),
                "address": fmt.get("secondaryText", {}).get("text", ""),
                "description": pred.get("text", {}).get("text", ""),
            })
        return out

    async def details(self, place_id: str, language: str = "en",
                      session_token: str | None = None) -> Place:
        params = {"languageCode": language}
        if session_token:
            params["sessionToken"] = session_token
        data = self._check(await self._client.get(
            f"/places/{quote(place_id, safe='')}", params=params, headers={"X-Goog-FieldMask": DETAILS_FIELDS}))
        maps_uri = data.get("googleMapsUri", "")
        match = CID_RE.search(maps_uri)
        if not match:
            raise PlacesAPIError(f"no cid in googleMapsUri {maps_uri!r} for {place_id}")
        cid = match.group(1)
        place = Place(
            google_id=cid_to_feature_id(cid),
            name=(data.get("displayName") or {}).get("text", ""),
            place_id=data.get("id", place_id),
            cid=cid,
            full_address=data.get("formattedAddress"),
            rating=data.get("rating"),
            reviews=data.get("userRatingCount"),
        )
        if self.cache:
            self.cache.put(place)
        return place
