"""Alpha Vantage's top-gainers / most-active list: one more tip source.

Prices do not come from here; Yahoo and Nasdaq are free with no daily cap.
The free key allows 25 requests a day and the list is cached for two hours,
so the agent uses about five. A throttled call still answers HTTP 200 with
an "Information" field instead of data, which is checked explicitly.

Key: the ALPHA environment variable, or ALPHA=yourkey in a gitignored .alpha file.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime
from typing import Any

from ..clock import ET
from .http import DataError, HttpClient

URL = "https://www.alphavantage.co/query"


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(str(x).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


def check_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DataError("alphavantage: unexpected response")
    for key in ("Note", "Information"):
        if key in payload and len(payload) <= 2:
            raise DataError(f"alphavantage limit or plan restriction: {str(payload[key])[:160]}")
    if "Error Message" in payload:
        raise DataError(f"alphavantage error: {str(payload['Error Message'])[:160]}")
    return payload


def parse_movers(payload: Any) -> list[dict[str, Any]]:
    p = check_payload(payload)
    out = []
    for key, kind in (("top_gainers", "gainers"), ("most_actively_traded", "active"), ("top_losers", "losers")):
        for r in p.get(key) or []:
            sym = (r.get("ticker") or "").upper()
            if not sym:
                continue
            out.append({
                "symbol": sym, "list": kind, "price": _f(r.get("price")),
                "change_pct": _f(r.get("change_percentage")), "volume": _f(r.get("volume")),
            })
    if not out:
        raise DataError("alphavantage: empty movers list")
    return out


class AlphaVantage:
    def __init__(self, http: HttpClient, api_key: str | None, daily_limit: int = 25, per_minute: int = 5):
        self.http = http
        self.api_key = api_key or ""
        self.daily_limit = daily_limit
        self.per_minute = max(per_minute, 1)
        self._lock = threading.Lock()
        self._recent: list[float] = []
        self._exhausted_day: date | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def remaining_today(self) -> int:
        today = datetime.now(ET).date()
        if self._exhausted_day == today:
            return 0
        return max(0, self.daily_limit - self.http.quota_used("alphavantage", today))

    def _call(self, params: dict[str, Any], ttl: float) -> Any:
        if not self.enabled:
            raise DataError("alphavantage: no key (set ALPHA, or put ALPHA=yourkey in a .alpha file)")
        full = {**params, "apikey": self.api_key}
        cached = self.http.cached_json(URL, full, ttl)
        if cached is not None:
            return cached
        today = datetime.now(ET).date()
        if self.remaining_today() <= 0:
            raise DataError(f"alphavantage: daily budget of {self.daily_limit} requests used")
        with self._lock:
            now = time.monotonic()
            self._recent = [t for t in self._recent if now - t < 60]
            if len(self._recent) >= self.per_minute:
                raise DataError(f"alphavantage: {self.per_minute}/minute limit, try again shortly")
            self._recent.append(now)
        self.http.quota_add("alphavantage", today)
        try:
            payload = self.http.get_json(URL, params=full, ttl=0)
            check_payload(payload)
        except DataError as exc:
            if "limit" in str(exc).lower():
                self._exhausted_day = today
            raise
        self.http.store_json(URL, full, payload)
        return payload

    def movers(self) -> list[dict[str, Any]]:
        return parse_movers(self._call({"function": "TOP_GAINERS_LOSERS"}, ttl=2 * 3600))
