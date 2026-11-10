"""Alpha Vantage: top movers, news with per-ticker sentiment, and a quote and
daily-bar fallback.

The free key allows 25 requests a day (5 a minute), and a throttled call
still answers HTTP 200, with a "Note" or "Information" field instead of data.
So every call goes through a persistent daily budget and the HTTP cache, and
the agent spends its calls where they matter most: the movers list for tips
and sentiment for the few best candidates. Premium keys raise the budget via
config (data.alphavantage_daily_limit / alphavantage_per_minute).

Set the key with the ALPHA (or ALPHAVANTAGE_API_KEY) environment variable, or
put ALPHA=yourkey in a .alpha file next to the project (it is gitignored).
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime
from typing import Any

import pandas as pd

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


def parse_global_quote(payload: Any) -> dict[str, Any]:
    q = check_payload(payload).get("Global Quote") or {}
    price = _f(q.get("05. price"))
    if not price:
        raise DataError("alphavantage: empty quote (unknown symbol?)")
    return {
        "symbol": q.get("01. symbol"),
        "open": _f(q.get("02. open")),
        "high": _f(q.get("03. high")),
        "low": _f(q.get("04. low")),
        "price": price,
        "volume": _f(q.get("06. volume")),
        "day": q.get("07. latest trading day"),
        "prev_close": _f(q.get("08. previous close")),
        "change_pct": _f(q.get("10. change percent")),
    }


def parse_daily(payload: Any) -> pd.DataFrame:
    series = check_payload(payload).get("Time Series (Daily)") or {}
    if not series:
        raise DataError("alphavantage: no daily series")
    rows = []
    for day, v in series.items():
        rows.append((
            pd.Timestamp(day, tz=ET),
            _f(v.get("1. open")), _f(v.get("2. high")), _f(v.get("3. low")), _f(v.get("4. close")),
            _f(v.get("5. volume")) or 0.0,
        ))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts").sort_index()
    df.index.name = None
    return df.dropna(subset=["close"]).astype(float)


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


def parse_news_sentiment(payload: Any, symbol: str) -> dict[str, Any]:
    """Relevance-weighted sentiment for one ticker plus its headlines."""
    p = check_payload(payload)
    headlines = []
    wsum = score = 0.0
    for item in p.get("feed") or []:
        pub = item.get("time_published")
        try:
            published = datetime.strptime(pub, "%Y%m%dT%H%M%S").replace(tzinfo=ET) if pub else None
        except ValueError:
            published = None
        headlines.append({"title": item.get("title", ""), "url": item.get("url"), "publisher": item.get("source"),
                          "published": published, "tickers": []})
        for ts in item.get("ticker_sentiment") or []:
            if (ts.get("ticker") or "").upper() != symbol.upper():
                continue
            rel = _f(ts.get("relevance_score")) or 0.0
            s = _f(ts.get("ticker_sentiment_score"))
            if s is not None and rel > 0:
                wsum += rel
                score += rel * s
    return {
        "articles": len(headlines),
        "sentiment": (score / wsum) if wsum else None,
        "relevance": wsum,
        "headlines": headlines,
    }


# Share of the daily allowance each use may spend, so a burst of one kind
# (say, price fallbacks while another source is down) cannot starve the rest.
BUDGET_SHARES = {"movers": 0.25, "news": 0.50, "prices": 0.25}


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

    def remaining_today(self, purpose: str | None = None) -> int:
        today = datetime.now(ET).date()
        if self._exhausted_day == today:
            return 0
        total_left = max(0, self.daily_limit - self.http.quota_used("alphavantage", today))
        if purpose is None:
            return total_left
        cap = max(1, int(round(self.daily_limit * BUDGET_SHARES.get(purpose, 1.0))))
        return min(total_left, max(0, cap - self.http.quota_used(f"alphavantage:{purpose}", today)))

    def _call(self, params: dict[str, Any], ttl: float, purpose: str = "prices") -> Any:
        if not self.enabled:
            raise DataError("alphavantage: no key (set ALPHA, or put ALPHA=yourkey in a .alpha file)")
        full = {**params, "apikey": self.api_key}
        cached = self.http.cached_json(URL, full, ttl)
        if cached is not None:
            return cached
        today = datetime.now(ET).date()
        if self.remaining_today() <= 0:
            raise DataError(f"alphavantage: daily budget of {self.daily_limit} requests used")
        if self.remaining_today(purpose) <= 0:
            raise DataError(f"alphavantage: today's {purpose} share of the {self.daily_limit}-request budget is used")
        with self._lock:
            now = time.monotonic()
            self._recent = [t for t in self._recent if now - t < 60]
            if len(self._recent) >= self.per_minute:
                raise DataError(f"alphavantage: {self.per_minute}/minute limit, try again shortly")
            self._recent.append(now)
        self.http.quota_add("alphavantage", today)
        self.http.quota_add(f"alphavantage:{purpose}", today)
        try:
            payload = self.http.get_json(URL, params=full, ttl=0)
            check_payload(payload)
        except DataError as exc:
            if "limit" in str(exc).lower():
                self._exhausted_day = today
            raise
        self.http.store_json(URL, full, payload)
        return payload

    def global_quote(self, symbol: str) -> dict[str, Any]:
        return parse_global_quote(self._call({"function": "GLOBAL_QUOTE", "symbol": symbol}, ttl=60))

    def daily(self, symbol: str, full: bool = False) -> pd.DataFrame:
        # outputsize=full may need a premium key; compact is the last 100 sessions.
        params = {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "full" if full else "compact"}
        try:
            return parse_daily(self._call(params, ttl=6 * 3600))
        except DataError as exc:
            if full and "premium" in str(exc).lower():
                return parse_daily(self._call({**params, "outputsize": "compact"}, ttl=6 * 3600))
            raise

    def movers(self) -> list[dict[str, Any]]:
        return parse_movers(self._call({"function": "TOP_GAINERS_LOSERS"}, ttl=2 * 3600, purpose="movers"))

    def news_sentiment(self, symbol: str) -> dict[str, Any]:
        return parse_news_sentiment(
            self._call({"function": "NEWS_SENTIMENT", "tickers": symbol, "sort": "LATEST", "limit": 50}, ttl=2 * 3600,
                       purpose="news"),
            symbol,
        )
