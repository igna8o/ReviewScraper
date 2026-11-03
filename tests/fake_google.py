"""A stand-in for Google's two endpoints, built from the documented positions.

It checks the requests the client sends (the feature ID, sort order and page
token inside ``reqpld``) and answers in the array layout the parser reads.
It does not prove Google still answers this way; the ``live`` tests do that.
"""

from __future__ import annotations

import json

import httpx

FID = "0x3442a8e1a4a5a6a7:0x1234abcd5678ef90"


def search_body(places: list[tuple[str, str]]) -> str:
    data: list = [None] * 65
    results = []
    for fid, name in places:
        record = [None] * 12
        record[10] = fid
        record[11] = name
        results.append([None, record])
    results.append([None, [None] * 12])  # an entry without a feature ID
    data[64] = results
    return ")]}'\n" + json.dumps(data)


def review_record(i: int, rating: int, millis: int, text: str = "Great place") -> list:
    record: list = [None] * 28
    record[1] = rating
    record[2] = [f"{i} weeks ago", None, str(millis)]
    record[3] = [f"Author {i}", None, f"https://www.google.com/maps/contrib/10{i}?hl=en", 12, 3]
    record[5] = f"review-{i}"
    record[26] = "en"
    record[27] = text
    return record


def review_body(records: list, token: str) -> str:
    block: list = [None] * 7
    block[2] = records
    block[6] = token
    outer: list = [None] * 11
    outer[10] = block
    return ")]}'\n" + json.dumps([None, outer])


class FakeGoogle:
    """Serves ``pages`` (a list of review-record lists) one token at a time."""

    def __init__(self, pages: list[list], places: list[tuple[str, str]] | None = None):
        self.pages = pages
        self.places = places or [(FID, "Test Place")]
        self.review_requests: list[dict] = []
        self.search_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path == "/search":
            self.search_requests.append(params)
            return httpx.Response(200, text=search_body(self.places))
        if request.url.path.endswith("/GetLocalBoqProxy"):
            payload = json.loads(params["reqpld"])
            args = payload[1][9]
            self.review_requests.append({"sort": args[1], "size": args[9], "fid": args[11][0], "token": args[19]})
            index = int(args[19]) if args[19] else 0
            next_token = str(index + 1) if index + 1 < len(self.pages) else ""
            return httpx.Response(200, text=review_body(self.pages[index], next_token))
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
