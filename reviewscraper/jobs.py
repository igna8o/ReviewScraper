"""Request archive and background runner, like Outscraper's ``/requests``.

Every API call gets a request ID. Async calls return the ID right away and run
in the background; their result is kept in SQLite for ``ttl`` seconds after
they finish, then deleted. A webhook URL, when given, receives a POST once the
request is done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("reviewscraper.jobs")

PENDING, SUCCESS, ERROR = "Pending", "Success", "Error"


class JobStore:
    def __init__(self, path: str = "reviewscraper.db", ttl: float = 2 * 3600,
                 max_concurrency: int = 4,
                 webhook_transport: httpx.AsyncBaseTransport | None = None):
        self.ttl = ttl
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                query TEXT NOT NULL,
                status TEXT NOT NULL,
                data TEXT,
                error TEXT,
                webhook TEXT,
                created_at REAL NOT NULL,
                finished_at REAL
            )"""
        )
        # Work that was running when the process stopped will never finish.
        self._db.execute(
            "UPDATE requests SET status=?, error=?, finished_at=? WHERE status=?",
            (ERROR, "server restarted before the request finished", time.time(), PENDING),
        )
        self._db.commit()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._webhook_transport = webhook_transport

    def close(self) -> None:
        self._db.close()

    # -- storage ---------------------------------------------------------- #

    def create(self, endpoint: str, query: list[str], webhook: str | None) -> str:
        self.purge()
        request_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO requests (id, endpoint, query, status, webhook, created_at) VALUES (?,?,?,?,?,?)",
            (request_id, endpoint, json.dumps(query), PENDING, webhook, time.time()),
        )
        self._db.commit()
        return request_id

    def finish(self, request_id: str, data: Any = None, error: str | None = None) -> None:
        self._db.execute(
            "UPDATE requests SET status=?, data=?, error=?, finished_at=? WHERE id=?",
            (ERROR if error else SUCCESS, None if error else json.dumps(data), error, time.time(), request_id),
        )
        self._db.commit()

    def get(self, request_id: str) -> dict[str, Any] | None:
        self.purge()
        row = self._db.execute(
            "SELECT id, endpoint, query, status, data, error, created_at, finished_at FROM requests WHERE id=?",
            (request_id,),
        ).fetchone()
        return self._row(row, with_data=True) if row else None

    def list(self, kind: str = "running", skip: int = 0, page_size: int = 25) -> list[dict[str, Any]]:
        self.purge()
        where = "status=?" if kind == "running" else "status!=?"
        rows = self._db.execute(
            f"SELECT id, endpoint, query, status, data, error, created_at, finished_at FROM requests "
            f"WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (PENDING, page_size, skip),
        ).fetchall()
        return [self._row(r, with_data=False) for r in rows]

    def purge(self) -> None:
        self._db.execute(
            "DELETE FROM requests WHERE finished_at IS NOT NULL AND finished_at < ?",
            (time.time() - self.ttl,),
        )
        self._db.commit()

    @staticmethod
    def _row(row: tuple, with_data: bool) -> dict[str, Any]:
        rid, endpoint, query, status, data, error, created, finished = row
        out: dict[str, Any] = {
            "id": rid,
            "endpoint": endpoint,
            "query": json.loads(query),
            "status": status,
            "created_at": created,
            "finished_at": finished,
        }
        if error:
            out["errorMessage"] = error
        if with_data and data is not None:
            out["data"] = json.loads(data)
        return out

    # -- running ---------------------------------------------------------- #

    async def run(self, request_id: str, work: Callable[[], Awaitable[Any]]) -> None:
        async with self._sem:
            try:
                data = await work()
            except Exception as exc:  # reported to the caller, not raised
                log.warning("request %s failed: %s", request_id, exc)
                self.finish(request_id, error=f"{type(exc).__name__}: {exc}")
            else:
                self.finish(request_id, data=data)

    def submit(self, request_id: str, work: Callable[[], Awaitable[Any]],
               webhook: str | None, results_location: str) -> None:
        async def job() -> None:
            await self.run(request_id, work)
            if webhook:
                await self._notify(webhook, request_id, results_location)

        task = asyncio.create_task(job())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, webhook: str, request_id: str, results_location: str) -> None:
        record = self.get(request_id) or {}
        data = record.get("data")
        payload = {
            "id": request_id,
            "status": record.get("status"),
            "results_location": results_location,
            "quantity": len(data) if isinstance(data, list) else 0,
        }
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._webhook_transport) as client:
                await client.post(webhook, json=payload)
        except httpx.HTTPError as exc:
            log.warning("webhook %s for %s failed: %s", webhook, request_id, exc)

    async def drain(self) -> None:
        """Wait for background requests (used by tests and on shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
