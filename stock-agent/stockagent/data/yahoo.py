"""Yahoo Finance: price history, quotes, predefined screeners and news.

These are the endpoints Yahoo's own pages use. They are unofficial: no key,
no SLA, and the shapes can change. Parsing is kept in pure functions so a
change shows up as a failing fixture test rather than as bad trades.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ..clock import ET
from .http import DataError, HttpClient

BASES = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")

# Yahoo's predefined screens that surface low-priced movers.
SCREENERS = (
    "day_gainers",
    "small_cap_gainers",
    "most_actives",
    "aggressive_small_caps",
)


def parse_chart(payload: dict[str, Any], intraday: bool = False) -> tuple[pd.DataFrame, dict[str, Any], list[dict]]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise DataError(f"yahoo chart error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise DataError("yahoo chart: empty result")
    res = results[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    df = pd.DataFrame(
        {
            "open": quote.get("open") or [None] * len(ts),
            "high": quote.get("high") or [None] * len(ts),
            "low": quote.get("low") or [None] * len(ts),
            "close": quote.get("close") or [None] * len(ts),
            "volume": quote.get("volume") or [None] * len(ts),
        },
        index=pd.to_datetime(ts, unit="s", utc=True).tz_convert(ET),
    )
    df = df.dropna(subset=["open", "high", "low", "close"]).astype(float)
    df["volume"] = df["volume"].fillna(0.0)
    if not intraday and len(df):
        # One row per session; Yahoo sometimes appends a live partial row with
        # a non-midnight timestamp for the current day.
        df.index = pd.DatetimeIndex([d.normalize() for d in df.index])
        df = df[~df.index.duplicated(keep="last")]
    splits = []
    for s in ((res.get("events") or {}).get("splits") or {}).values():
        splits.append(
            {
                "date": datetime.fromtimestamp(int(s["date"]), tz=timezone.utc).astimezone(ET).date(),
                "numerator": float(s.get("numerator", 1)),
                "denominator": float(s.get("denominator", 1)),
            }
        )
    return df, meta, splits


def parse_quotes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    resp = payload.get("quoteResponse") or {}
    if resp.get("error"):
        raise DataError(f"yahoo quote error: {resp['error']}")
    return {q["symbol"]: q for q in resp.get("result") or [] if q.get("symbol")}


def parse_screener(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fin = payload.get("finance") or {}
    if fin.get("error"):
        raise DataError(f"yahoo screener error: {fin['error']}")
    out: list[dict[str, Any]] = []
    for res in fin.get("result") or []:
        for q in res.get("quotes") or []:
            if q.get("quoteType", "EQUITY") != "EQUITY" or not q.get("symbol"):
                continue
            out.append(
                {
                    "symbol": q["symbol"],
                    "name": q.get("shortName") or q.get("longName"),
                    "price": q.get("regularMarketPrice"),
                    "change_pct": q.get("regularMarketChangePercent"),
                    "volume": q.get("regularMarketVolume"),
                    "avg_volume": q.get("averageDailyVolume3Month"),
                    "market_cap": q.get("marketCap"),
                    "shares_outstanding": q.get("sharesOutstanding"),
                    "exchange": q.get("fullExchangeName") or q.get("exchange"),
                }
            )
    return out


def parse_news(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for n in payload.get("news") or []:
        ts = n.get("providerPublishTime")
        out.append(
            {
                "title": n.get("title", ""),
                "publisher": n.get("publisher"),
                "url": n.get("link"),
                "published": datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None,
                "tickers": n.get("relatedTickers") or [],
            }
        )
    return out


class Yahoo:
    def __init__(self, http: HttpClient):
        self.http = http
        self._crumb: str | None = None

    def _get(self, path: str, params: dict[str, Any], ttl: float, crumb: bool = False) -> Any:
        last: Exception | None = None
        for base in BASES:
            p = dict(params)
            if crumb:
                p["crumb"] = self._get_crumb()
            try:
                return self.http.get_json(base + path, params=p, ttl=ttl)
            except DataError as exc:
                last = exc
                if crumb and ("401" in str(exc) or "403" in str(exc)):
                    self._crumb = None
        raise DataError(str(last))

    def _get_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        # The cookie Yahoo wants is set by any request to fc.yahoo.com, which answers 404.
        try:
            self.http.get_text("https://fc.yahoo.com", allow_status=(404,))
        except DataError:
            pass
        crumb = self.http.get_text(BASES[0] + "/v1/test/getcrumb").strip()
        if not crumb or "<" in crumb or len(crumb) > 64:
            raise DataError("yahoo: could not obtain crumb")
        self._crumb = crumb
        return crumb

    def chart(self, symbol: str, range_: str = "6mo", interval: str = "1d", prepost: bool = False, ttl: float = 300):
        payload = self._get(
            f"/v8/finance/chart/{symbol}",
            {"range": range_, "interval": interval, "includePrePost": str(prepost).lower(), "events": "div,splits"},
            ttl=ttl,
        )
        return parse_chart(payload, intraday=not interval.endswith(("d", "wk", "mo")))

    def chart_period(self, symbol: str, start: datetime, end: datetime, interval: str = "1d", ttl: float = 86400):
        payload = self._get(
            f"/v8/finance/chart/{symbol}",
            {"period1": int(start.timestamp()), "period2": int(end.timestamp()), "interval": interval, "events": "div,splits"},
            ttl=ttl,
        )
        return parse_chart(payload, intraday=not interval.endswith(("d", "wk", "mo")))

    def quotes(self, symbols: list[str], ttl: float = 15) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i : i + 50]
            payload = self._get("/v7/finance/quote", {"symbols": ",".join(chunk)}, ttl=ttl, crumb=True)
            out.update(parse_quotes(payload))
        return out

    def screener(self, scr_id: str, count: int = 100, ttl: float = 300) -> list[dict[str, Any]]:
        payload = self._get(
            "/v1/finance/screener/predefined/saved", {"scrIds": scr_id, "count": count}, ttl=ttl, crumb=True
        )
        return parse_screener(payload)

    def news(self, symbol: str, count: int = 10, ttl: float = 900) -> list[dict[str, Any]]:
        payload = self._get(
            "/v1/finance/search", {"q": symbol, "newsCount": count, "quotesCount": 0, "listsCount": 0}, ttl=ttl
        )
        return parse_news(payload)
