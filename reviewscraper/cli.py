"""Command line: run the API server, or fetch reviews straight to JSON/CSV.

    reviewscraper serve --port 8000
    reviewscraper reviews "Din Tai Fung, Taipei" --limit 50 --sort newest -o reviews.csv
    reviewscraper search "pizza in Madrid"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from typing import Any

from .providers.google_maps import SORTS, GoogleMaps


def _write(rows: list[dict[str, Any]], output: str | None) -> None:
    if output and output.endswith(".csv"):
        keys: list[str] = []
        for row in rows:
            keys.extend(k for k in row if k not in keys)
        with open(output, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        return
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)


async def _reviews(args: argparse.Namespace) -> None:
    maps = GoogleMaps(min_delay=args.min_delay, max_delay=args.max_delay)
    try:
        places = await maps.place_reviews(
            args.query, limit=args.places, region=args.region, reviews_limit=args.limit,
            sort=args.sort, language=args.language, ignore_empty=args.ignore_empty,
            cutoff=args.cutoff, reviews_query=args.reviews_query,
        )
    finally:
        await maps.aclose()
    if args.output and args.output.endswith(".csv"):
        rows = []
        for place in places:
            for review in place["reviews_data"]:
                rows.append({"place_name": place["name"], "google_id": place["google_id"], **review})
        _write(rows, args.output)
    else:
        _write(places, args.output)


async def _search(args: argparse.Namespace) -> None:
    maps = GoogleMaps()
    try:
        places = await maps.search(args.query, language=args.language, region=args.region, limit=args.limit)
    finally:
        await maps.aclose()
    _write([p.as_dict() for p in places], args.output)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reviewscraper")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    rev = sub.add_parser("reviews", help="fetch Google Maps reviews")
    rev.add_argument("query", nargs="+", help="place name, search, feature ID or Maps URL")
    rev.add_argument("--limit", type=int, default=10, help="reviews per place (0 = all)")
    rev.add_argument("--places", type=int, default=1, help="places per search query")
    rev.add_argument("--sort", choices=list(SORTS), default="most_relevant")
    rev.add_argument("--language", default="en")
    rev.add_argument("--region")
    rev.add_argument("--cutoff", type=int, help="oldest review timestamp to keep (Unix seconds)")
    rev.add_argument("--reviews-query", help="keep only reviews whose text contains this")
    rev.add_argument("--ignore-empty", action="store_true")
    rev.add_argument("--min-delay", type=float, default=1.0)
    rev.add_argument("--max-delay", type=float, default=3.0)
    rev.add_argument("-o", "--output", help="file to write (.json or .csv); stdout if omitted")

    srch = sub.add_parser("search", help="search Google Maps places")
    srch.add_argument("query")
    srch.add_argument("--limit", type=int, default=20)
    srch.add_argument("--language", default="en")
    srch.add_argument("--region")
    srch.add_argument("-o", "--output")

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn
        uvicorn.run("reviewscraper.api:app", host=args.host, port=args.port)
    elif args.command == "reviews":
        asyncio.run(_reviews(args))
    else:
        asyncio.run(_search(args))


if __name__ == "__main__":
    main(sys.argv[1:])
