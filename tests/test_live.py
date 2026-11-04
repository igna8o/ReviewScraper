"""Checks against Google itself. Run with: pytest -m live

These are the only tests that show the request shapes still work; the others
use a stand-in built from the documented response layout.

Google often answers the search request from cloud/datacenter IPs with a
CAPTCHA page (HTTP 429). The review test uses a fixed feature ID so it does
not depend on search.
"""

import os

import pytest

from reviewscraper.providers.google_maps import GoogleMaps
from reviewscraper.providers.places import PlacesAPI

pytestmark = pytest.mark.live

# Empire State Building, New York
FEATURE_ID = os.environ.get("REVIEWSCRAPER_LIVE_FID", "0x89c259a9b3117469:0xd134e199a405a163")
QUERY = os.environ.get("REVIEWSCRAPER_LIVE_QUERY", "Empire State Building, New York")


async def test_live_two_pages_of_newest_reviews():
    maps = GoogleMaps(min_delay=1, max_delay=2)
    try:
        reviews = await maps.reviews(FEATURE_ID, reviews_limit=20, sort="newest")
    finally:
        await maps.aclose()
    assert len(reviews) == 20
    assert len({r["review_id"] for r in reviews}) == 20
    assert all(r["review_rating"] in range(1, 6) for r in reviews)
    assert all(r["author_title"] and r["author_id"] and r["review_timestamp"] for r in reviews)
    timestamps = [r["review_timestamp"] for r in reviews]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_live_search():
    maps = GoogleMaps()
    try:
        places = await maps.search(QUERY, limit=3)
    finally:
        await maps.aclose()
    assert places and all(p.google_id and p.name for p in places)


@pytest.mark.skipif(not os.environ.get("GOOGLE_PLACES_API_KEY"), reason="GOOGLE_PLACES_API_KEY is not set")
async def test_live_places_to_reviews():
    """Onboarding path: type a name -> pick a suggestion -> details -> reviews.

    Uses the official Places API (key from the environment, never printed) and
    checks that the CID in ``googleMapsUri`` converted to ``0x0:0x...`` is
    accepted by the review endpoint and returns real reviews.
    """
    places = PlacesAPI()
    maps = GoogleMaps(min_delay=1, max_delay=2)
    try:
        suggestions = await places.autocomplete("Empire State Building", region="us")
        assert suggestions and all(s["place_id"] and s["name"] for s in suggestions)
        match = next(s for s in suggestions if "New York" in s["address"] or "New York" in s["description"])

        place = await places.details(match["place_id"])
        assert place.name == "Empire State Building"
        assert place.cid and place.google_id == f"0x0:{int(place.cid):#x}"
        assert place.google_id.split(":")[1] == FEATURE_ID.split(":")[1]
        assert place.rating and place.reviews and place.reviews > 1000

        reviews = await maps.reviews(place.google_id, reviews_limit=10, sort="newest")
    finally:
        await places.aclose()
        await maps.aclose()
    assert len(reviews) == 10
    assert len({r["review_id"] for r in reviews}) == 10
    assert all(r["review_rating"] in range(1, 6) and r["author_title"] for r in reviews)
