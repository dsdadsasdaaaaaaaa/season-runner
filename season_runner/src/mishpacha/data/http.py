"""Cached HTTP layer.

Every external fetch goes through here so that (a) we never hammer a provider
hard enough to get blocked, and (b) a draft-day outage at any single source
degrades to stale-but-usable data instead of a crash.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache"
CACHE_DB = CACHE_DIR / "http.sqlite"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    status     INTEGER NOT NULL,
    body       BLOB
);
CREATE INDEX IF NOT EXISTS idx_http_fetched ON http_cache(fetched_at);
"""


def _conn() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(CACHE_DB)
    c.executescript(_SCHEMA)
    return c


def _key(url: str, params: dict | None) -> str:
    raw = url + "|" + json.dumps(params or {}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


# Seconds to wait after each failed attempt; the last entry is 0 (give up).
RETRY_PAUSES = (2.0, 5.0, 12.0, 0.0)


def get(
    url: str,
    *,
    params: dict | None = None,
    ttl: float = 3600.0,
    headers: dict | None = None,
    timeout: float = 30.0,
    force: bool = False,
) -> tuple[int, bytes]:
    """GET with a sqlite-backed TTL cache. Returns (status, body).

    On a network failure we fall back to any cached body regardless of age --
    stale data beats no data when the draft clock is running.
    """
    k = _key(url, params)
    now = time.time()

    with _conn() as c:
        row = c.execute(
            "SELECT fetched_at, status, body FROM http_cache WHERE key=?", (k,)
        ).fetchone()

    if row and not force and (now - row[0]) < ttl:
        return row[1], row[2]

    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "application/json, text/plain, */*"}
    for name, value in (headers or {}).items():
        if value is None:
            hdrs.pop(name, None)       # None removes a default header entirely
        else:
            hdrs[name] = value

    # Transient failures are retried before giving up. On 2026-09-27 the Mac
    # had just woken, DNS was not up yet ("nodename nor servname provided"),
    # and a single failed read crashed a lineup job whose swap had already
    # landed. A laptop waking from sleep needs a few seconds, not a traceback.
    last_exc: Exception | None = None
    status, body = 0, b""
    # With a cached copy to fall back on there is no reason to wait: one try,
    # then serve stale. Only a read with nothing behind it is worth retrying.
    for pause in (RETRY_PAUSES if row is None else (0.0,)):
        try:
            r = httpx.get(url, params=params, headers=hdrs, timeout=timeout,
                          follow_redirects=True)
            status, body = r.status_code, r.content
            last_exc = None
            if status < 500:
                break
        except (httpx.TransportError, OSError) as exc:
            last_exc = exc
        if pause:
            time.sleep(pause)
    if last_exc is not None:
        if row:
            return row[1], row[2]  # stale beats nothing
        raise last_exc

    if status == 200:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?)",
                (k, url, now, status, body),
            )
    elif row:
        return row[1], row[2]

    return status, body


def get_json(url: str, **kw) -> Any:
    status, body = get(url, **kw)
    if status != 200:
        raise RuntimeError(f"HTTP {status} for {url}")
    if not body:
        return None
    return json.loads(body)


def cache_stats() -> dict:
    with _conn() as c:
        n, oldest, newest = c.execute(
            "SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM http_cache"
        ).fetchone()
    return {"entries": n, "oldest": oldest, "newest": newest, "db": str(CACHE_DB)}
