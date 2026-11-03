import json

import httpx
import pytest

from reviewscraper.http import GoogleHTTP
from reviewscraper.providers.google_maps import (
    FormatChanged,
    GoogleMaps,
    parse_review_page,
    parse_search,
    resolve_feature_id,
    review_params,
)

from .fake_google import FID, FakeGoogle, review_body, review_record, search_body

DAY_MS = 86_400_000
NOW_MS = 1_790_000_000_000


def make_maps(fake: FakeGoogle) -> GoogleMaps:
    return GoogleMaps(http=GoogleHTTP(transport=fake.transport(), retries=0), min_delay=0, max_delay=0)


def test_parse_review_page_reads_fields():
    record = review_record(1, 4, NOW_MS, "Nice &amp; tasty<br>Would return")
    reviews, token = parse_review_page(review_body([record], "next-token"))
    assert token == "next-token"
    assert reviews == [{
        "review_id": "review-1",
        "author_title": "Author 1",
        "author_id": "101",
        "author_link": "https://www.google.com/maps/contrib/101?hl=en",
        "author_reviews_count": 12,
        "author_photos_count": 3,
        "review_text": "Nice & tasty\nWould return",
        "review_rating": 4,
        "review_timestamp": NOW_MS // 1000,
        "review_datetime_utc": "09/21/2026 14:13:20",
        "review_relative_time": "1 weeks ago",
        "review_language": "en",
        "review_pagination_id": "next-token",
    }]


def test_parse_review_page_without_block_is_format_change():
    with pytest.raises(FormatChanged):
        parse_review_page(")]}'\n" + json.dumps([None, [None] * 3]))
    with pytest.raises(FormatChanged):
        parse_review_page("<html>sorry</html>")


def test_parse_search_keeps_order_and_drops_duplicates():
    other = "0xabc:0xdef"
    places = parse_search(search_body([(FID, "A"), (other, "B"), (FID, "A again")]))
    assert [(p.google_id, p.name) for p in places] == [(FID, "A"), (other, "B")]
    with pytest.raises(FormatChanged):
        parse_search(")]}'\n[]")


def test_review_params_put_values_where_google_reads_them():
    args = json.loads(review_params(FID, 2, "tok", "es")["reqpld"])[1][9]
    assert (args[1], args[9], args[11], args[19]) == (2, 10, [FID], "tok")


def test_resolve_feature_id_from_url_and_plain_id():
    url = f"https://www.google.com/maps/place/X/@1,2,17z/data=!4m6!3m5!1s{FID}!8m2"
    assert resolve_feature_id(url) == FID
    assert resolve_feature_id(FID) == FID
    assert resolve_feature_id("pizza in Madrid") is None


async def test_reviews_follow_pages_until_limit():
    pages = [[review_record(p * 10 + i, 5, NOW_MS - (p * 10 + i) * DAY_MS) for i in range(10)] for p in range(3)]
    fake = FakeGoogle(pages)
    maps = make_maps(fake)
    reviews = await maps.reviews(FID, reviews_limit=25, sort="newest")
    await maps.aclose()
    assert len(reviews) == 25
    assert [r["token"] for r in fake.review_requests] == ["", "1", "2"]
    assert {r["sort"] for r in fake.review_requests} == {2}


async def test_reviews_zero_limit_reads_every_page():
    fake = FakeGoogle([[review_record(i, 5, NOW_MS)] for i in range(4)])
    maps = make_maps(fake)
    assert len(await maps.reviews(FID, reviews_limit=0)) == 4
    await maps.aclose()


async def test_cutoff_stops_at_older_reviews_and_forces_newest():
    pages = [[review_record(i, 5, NOW_MS - i * DAY_MS) for i in range(10)], [review_record(99, 5, 0)]]
    fake = FakeGoogle(pages)
    maps = make_maps(fake)
    reviews = await maps.reviews(FID, reviews_limit=0, cutoff=(NOW_MS - 3 * DAY_MS) // 1000)
    await maps.aclose()
    assert [r["review_id"] for r in reviews] == ["review-0", "review-1", "review-2", "review-3"]
    assert len(fake.review_requests) == 1  # stopped without reading page 2
    assert fake.review_requests[0]["sort"] == 2


async def test_filters_ignore_empty_and_reviews_query():
    page = [review_record(1, 5, NOW_MS, ""), review_record(2, 5, NOW_MS, "Amazing pasta"),
            review_record(3, 5, NOW_MS, "ok")]
    maps = make_maps(FakeGoogle([page]))
    assert [r["review_id"] for r in await maps.reviews(FID, ignore_empty=True)] == ["review-2", "review-3"]
    assert [r["review_id"] for r in await maps.reviews(FID, reviews_query="PASTA")] == ["review-2"]
    await maps.aclose()


async def test_cutoff_rating_for_lowest_rating():
    page = [review_record(1, 1, NOW_MS), review_record(2, 2, NOW_MS), review_record(3, 3, NOW_MS)]
    maps = make_maps(FakeGoogle([page]))
    reviews = await maps.reviews(FID, sort="lowest_rating", cutoff_rating=2)
    await maps.aclose()
    assert [r["review_rating"] for r in reviews] == [1, 2]


async def test_place_reviews_searches_text_queries():
    fake = FakeGoogle([[review_record(1, 5, NOW_MS)]], places=[(FID, "Test Place"), ("0x1:0x2", "Other")])
    maps = make_maps(fake)
    places = await maps.place_reviews(["test place madrid"], limit=1, language="es", region="ES")
    await maps.aclose()
    assert places[0]["name"] == "Test Place"
    assert places[0]["google_id"] == FID
    assert places[0]["reviews_scraped"] == 1
    assert fake.search_requests[0]["q"] == "test place madrid"
    assert fake.search_requests[0]["gl"] == "es"


async def test_http_errors_are_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    maps = GoogleMaps(http=GoogleHTTP(transport=httpx.MockTransport(handler), retries=0))
    with pytest.raises(Exception, match="HTTP 403"):
        await maps.reviews(FID)
    await maps.aclose()
