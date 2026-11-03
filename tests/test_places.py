import json

import httpx
import pytest
from fastapi.testclient import TestClient

from reviewscraper.api import create_app
from reviewscraper.http import GoogleHTTP
from reviewscraper.jobs import JobStore
from reviewscraper.providers.google_maps import GoogleMaps, cid_to_feature_id, resolve_feature_id
from reviewscraper.providers.places import PlacesAPI

from .fake_google import FakeGoogle, review_record

CID = "15074921902713971043"  # 0xd134e199a405a163, Empire State Building
FID = "0x0:0xd134e199a405a163"


def test_cid_to_feature_id():
    assert cid_to_feature_id(CID) == FID


@pytest.mark.parametrize("query", [
    f"https://maps.google.com/?cid={CID}",
    f"https://www.google.com/maps?cid={CID}&hl=es",
    f"https://www.google.com/search?q=x&ludocid={CID}",
    f"cid:{CID}",
])
def test_resolve_cid_links(query):
    assert resolve_feature_id(query) == FID


class FakePlaces:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["X-Goog-Api-Key"] == "k"
        if request.url.path == "/v1/places:autocomplete":
            body = json.loads(request.content)
            assert body["input"] == "empire"
            return httpx.Response(200, json={"suggestions": [{"placePrediction": {
                "placeId": "ChIJaXQRs6lZwokRY6EFpJnhNNE",
                "text": {"text": "Empire State Building, 5th Avenue, New York"},
                "structuredFormat": {"mainText": {"text": "Empire State Building"},
                                     "secondaryText": {"text": "5th Avenue, New York"}},
            }}, {"queryPrediction": {"text": {"text": "empire state"}}}]})
        if request.url.path == "/v1/places/ChIJaXQRs6lZwokRY6EFpJnhNNE":
            assert "googleMapsUri" in request.headers["X-Goog-FieldMask"]
            return httpx.Response(200, json={
                "id": "ChIJaXQRs6lZwokRY6EFpJnhNNE",
                "displayName": {"text": "Empire State Building", "languageCode": "en"},
                "formattedAddress": "20 W 34th St., New York, NY 10001, USA",
                "googleMapsUri": f"https://maps.google.com/?cid={CID}",
                "rating": 4.7, "userRatingCount": 98000,
            })
        return httpx.Response(404, json={})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEWSCRAPER_API_KEYS", raising=False)
    fake_places = FakePlaces()
    places = PlacesAPI(api_key="k", transport=httpx.MockTransport(fake_places.handler))
    fake = FakeGoogle([[review_record(1, 5, 1_790_000_000_000)]])
    maps = GoogleMaps(http=GoogleHTTP(transport=fake.transport(), retries=0),
                      min_delay=0, max_delay=0, places=places)
    with TestClient(create_app(maps=maps, jobs=JobStore(str(tmp_path / "j.db")))) as c:
        c.fake = fake
        yield c


def test_autocomplete(client):
    body = client.get("/places/autocomplete", params={"input": "empire"}).json()
    assert body["data"] == [{
        "place_id": "ChIJaXQRs6lZwokRY6EFpJnhNNE",
        "name": "Empire State Building",
        "address": "5th Avenue, New York",
        "description": "Empire State Building, 5th Avenue, New York",
    }]


def test_details_gives_feature_id(client):
    data = client.get("/places/details", params={"placeId": "ChIJaXQRs6lZwokRY6EFpJnhNNE"}).json()["data"]
    assert data["google_id"] == FID
    assert data["cid"] == CID
    assert data["full_address"].startswith("20 W 34th")
    assert (data["rating"], data["reviews"]) == (4.7, 98000)


def test_reviews_by_place_id(client):
    body = client.get("/maps/reviews-v3", params={"query": "ChIJaXQRs6lZwokRY6EFpJnhNNE", "async": "false"}).json()
    [place] = body["data"]
    assert place["google_id"] == FID and place["name"] == "Empire State Building"
    assert len(place["reviews_data"]) == 1
    assert client.fake.review_requests[0]["fid"] == FID


def test_places_endpoints_need_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("REVIEWSCRAPER_API_KEYS", raising=False)
    maps = GoogleMaps(http=GoogleHTTP(transport=FakeGoogle([[]]).transport()))
    with TestClient(create_app(maps=maps, jobs=JobStore(str(tmp_path / "j.db")))) as c:
        assert c.get("/places/autocomplete", params={"input": "x"}).status_code == 501
