"""HTTP with a disk cache, per-host pacing and retries.

Free market-data endpoints rate-limit hard and go down without notice. Every
source goes through here so a failure is one logged DataError, never a crash
in the trading loop.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests


class DataError(Exception):
    pass


class HttpClient:
    def __init__(
        self,
        cache_path: str | None,
        user_agent: str,
        timeout: float = 15.0,
        min_interval: float = 0.35,
        retries: int = 3,
    ):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self._last_hit: dict[str, float] = {}
        # Circuit breaker: a host that refused the connection is skipped for a
        # while, so one dead source cannot stall every tick of the trading loop.
        self._down_until: dict[str, float] = {}
        self.down_seconds = 300.0
        self._lock = threading.Lock()
        self._cache: sqlite3.Connection | None = None
        if cache_path:
            if cache_path != ":memory:":
                Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            self._cache = sqlite3.connect(cache_path, check_same_thread=False)
            self._cache.execute("CREATE TABLE IF NOT EXISTS http_cache (key TEXT PRIMARY KEY, ts REAL, body TEXT)")
        self.stats: dict[str, dict[str, int]] = {}

    def _pace(self, host: str) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_hit.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def _cache_get(self, key: str, ttl: float) -> str | None:
        if not self._cache or ttl <= 0:
            return None
        with self._lock:
            row = self._cache.execute("SELECT ts, body FROM http_cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[0] <= ttl:
            return row[1]
        return None

    def _cache_put(self, key: str, body: str) -> None:
        if not self._cache:
            return
        with self._lock:
            self._cache.execute(
                "INSERT INTO http_cache(key, ts, body) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, body=excluded.body",
                (key, time.time(), body),
            )
            self._cache.commit()

    def _count(self, host: str, outcome: str) -> None:
        self.stats.setdefault(host, {}).setdefault(outcome, 0)
        self.stats[host][outcome] += 1

    def get_text(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ttl: float = 0,
        allow_status: tuple[int, ...] = (),
    ) -> str:
        full = url + ("?" + urlencode(params, doseq=True) if params else "")
        cached = self._cache_get(full, ttl)
        if cached is not None:
            return cached
        host = urlparse(url).netloc
        if time.monotonic() < self._down_until.get(host, 0.0):
            raise DataError(f"{host} skipped: unreachable recently")
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._pace(host)
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except (requests.exceptions.ProxyError, requests.exceptions.SSLError) as exc:
                # A proxy or TLS refusal will not change on retry.
                self._count(host, "refused")
                self._down_until[host] = time.monotonic() + self.down_seconds
                raise DataError(f"{host} refused the connection: {exc}") from exc
            except requests.RequestException as exc:
                last_err = exc
                self._count(host, "error")
                if attempt + 1 < self.retries:
                    time.sleep(min(8.0, 1.5 * 2**attempt))
                continue
            if resp.status_code == 200 or resp.status_code in allow_status:
                self._count(host, "ok")
                if resp.status_code == 200:
                    self._cache_put(full, resp.text)
                return resp.text
            self._count(host, str(resp.status_code))
            last_err = DataError(f"{host} returned HTTP {resp.status_code}")
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * 2**attempt
                time.sleep(min(delay, 15.0))
                continue
            break
        if not isinstance(last_err, DataError):
            self._down_until[host] = time.monotonic() + self.down_seconds
        raise DataError(f"GET {url} failed: {last_err}")

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, ttl: float = 0) -> Any:
        text = self.get_text(url, params=params, headers=headers, ttl=ttl)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataError(f"{url} did not return JSON: {text[:120]!r}") from exc
