"""Checks against Google itself. Run with: pytest -m live

These are the only tests that show the request shapes still work; the others
use a stand-in built from the documented response layout.
"""

import os

import pytest

from reviewscraper.providers.google_maps import GoogleMaps

pytestmark = pytest.mark.live

QUERY = os.environ.get("REVIEWSCRAPER_LIVE_QUERY", "Empire State Building, New York")


async def test_live_search_and_two_pages_of_reviews():
    maps = GoogleMaps(min_delay=1, max_delay=2)
    try:
        places = await maps.search(QUERY, limit=1)
        assert places, "search returned no places"
        reviews = await maps.reviews(places[0].google_id, reviews_limit=20, sort="newest")
    finally:
        await maps.aclose()
    assert len(reviews) == 20
    assert all(r["review_rating"] in range(1, 6) for r in reviews)
    assert all(r["review_id"] and r["author_title"] for r in reviews)
    timestamps = [r["review_timestamp"] for r in reviews]
    assert timestamps == sorted(timestamps, reverse=True)
