import time

import httpx
import pytest
from fastapi.testclient import TestClient

from reviewscraper.api import create_app
from reviewscraper.http import GoogleHTTP
from reviewscraper.jobs import JobStore
from reviewscraper.providers.google_maps import GoogleMaps

from .fake_google import FID, FakeGoogle, review_record

NOW_MS = 1_790_000_000_000


@pytest.fixture
def webhook_calls():
    return []


@pytest.fixture
def client(tmp_path, monkeypatch, webhook_calls):
    monkeypatch.setenv("REVIEWSCRAPER_API_KEYS", "secret")
    fake = FakeGoogle([[review_record(i, 5, NOW_MS) for i in range(10)],
                       [review_record(i, 4, NOW_MS) for i in range(10, 15)]])
    maps = GoogleMaps(http=GoogleHTTP(transport=fake.transport(), retries=0), min_delay=0, max_delay=0)

    def hook(request: httpx.Request) -> httpx.Response:
        webhook_calls.append((str(request.url), request.read()))
        return httpx.Response(200)

    jobs = JobStore(str(tmp_path / "jobs.db"), webhook_transport=httpx.MockTransport(hook))
    with TestClient(create_app(maps=maps, jobs=jobs), headers={"X-API-KEY": "secret"}) as c:
        yield c


def wait_done(client, request_id):
    for _ in range(100):
        record = client.get(f"/requests/{request_id}").json()
        if record["status"] != "Pending":
            return record
        time.sleep(0.02)
    raise AssertionError("request never finished")


def test_requires_api_key(client):
    response = client.get("/maps/reviews-v3", params={"query": FID}, headers={"X-API-KEY": "wrong"})
    assert response.status_code == 401


def test_sync_reviews(client):
    response = client.get("/maps/reviews-v3", params={"query": FID, "reviewsLimit": 12, "async": "false"})
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "Success"
    [place] = body["data"]
    assert place["google_id"] == FID
    assert len(place["reviews_data"]) == 12


def test_async_reviews_with_webhook(client, webhook_calls):
    response = client.get("/maps/reviews-v3", params={
        "query": FID, "reviewsLimit": 0, "webhook": "https://example.test/hook"})
    body = response.json()
    assert body["status"] == "Pending"
    assert body["results_location"].endswith(f"/requests/{body['id']}")
    record = wait_done(client, body["id"])
    assert record["status"] == "Success"
    assert len(record["data"][0]["reviews_data"]) == 15
    for _ in range(50):
        if webhook_calls:
            break
        time.sleep(0.02)
    assert webhook_calls and webhook_calls[0][0] == "https://example.test/hook"
    assert body["id"].encode() in webhook_calls[0][1]


def test_fields_filter(client):
    body = client.get("/maps/reviews-v3", params={
        "query": FID, "async": "false", "fields": "name,review_text,review_rating"}).json()
    [place] = body["data"]
    assert set(place) == {"name", "reviews_data"}
    assert set(place["reviews_data"][0]) == {"review_text", "review_rating"}


def test_search(client):
    body = client.get("/maps/search-v3", params={"query": "test place", "async": "false"}).json()
    assert body["data"] == [[{
        "query": "test place", "name": "Test Place", "google_id": FID,
        "location_link": f"https://www.google.com/maps/place/data=!4m2!3m1!1s{FID}",
    }]]


def test_bad_sort_is_rejected(client):
    assert client.get("/maps/reviews-v3", params={"query": FID, "sort": "random"}).status_code == 422


def test_unknown_request_is_404(client):
    assert client.get("/requests/nope").status_code == 404


def test_finished_requests_are_listed(client):
    rid = client.get("/maps/reviews-v3", params={"query": FID, "async": "false"}).json()["id"]
    finished = client.get("/requests", params={"type": "finished"}).json()
    assert [r["id"] for r in finished] == [rid]
