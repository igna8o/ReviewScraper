"""Official Google Places API (New): business search for onboarding.

Used so a customer can type their business name and pick it from a list,
without scraping Google Search (which answers cloud IPs with a CAPTCHA).
Place Details returns ``googleMapsUri`` (``https://maps.google.com/?cid=...``),
and the CID becomes the feature ID the review endpoint accepts.

Needs ``GOOGLE_PLACES_API_KEY`` with "Places API (New)" enabled in Google
Cloud. Request and response shapes follow Google's documentation for
``places:autocomplete`` and ``places/{id}``; they were not tested live from
the environment this was written in.

Pass the same ``session_token`` (any UUID) to the autocomplete calls and the
final details call of one search so Google bills them as one session.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .google_maps import CID_RE, Place, cid_to_feature_id

PLACES_URL = "https://places.googleapis.com/v1"
DETAILS_FIELDS = "id,displayName,formattedAddress,googleMapsUri,rating,userRatingCount"


class PlacesAPIError(RuntimeError):
    pass


class PlacesAPI:
    def __init__(self, api_key: str | None = None, timeout: float = 15,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not self.api_key:
            raise ValueError("GOOGLE_PLACES_API_KEY is not set")
        self._client = httpx.AsyncClient(base_url=PLACES_URL, timeout=timeout, transport=transport,
                                         headers={"X-Goog-Api-Key": self.api_key})

    @classmethod
    def from_env(cls) -> "PlacesAPI | None":
        return cls() if os.environ.get("GOOGLE_PLACES_API_KEY") else None

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _check(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            raise PlacesAPIError(f"Places API HTTP {response.status_code}: {response.text[:300]}")
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
            f"/places/{place_id}", params=params, headers={"X-Goog-FieldMask": DETAILS_FIELDS}))
        maps_uri = data.get("googleMapsUri", "")
        match = CID_RE.search(maps_uri)
        if not match:
            raise PlacesAPIError(f"no cid in googleMapsUri {maps_uri!r} for {place_id}")
        cid = match.group(1)
        return Place(
            google_id=cid_to_feature_id(cid),
            name=(data.get("displayName") or {}).get("text", ""),
            place_id=data.get("id", place_id),
            cid=cid,
            full_address=data.get("formattedAddress"),
            rating=data.get("rating"),
            reviews=data.get("userRatingCount"),
        )
