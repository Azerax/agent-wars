"""Nasdaq's stock screener: every listed US common stock with last price,
change and volume in one request. This is the agent's universe."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .http import HttpClient

URL = "https://api.nasdaq.com/api/screener/stocks"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _num(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^0-9.\-]", "", str(text))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_screener(payload: dict[str, Any], exchange: str | None = None) -> pd.DataFrame:
    data = payload.get("data") or {}
    rows = data.get("rows") or (data.get("table") or {}).get("rows") or []
    records = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        records.append(
            {
                "symbol": sym,
                "name": r.get("name"),
                "price": _num(r.get("lastsale")),
                "change": _num(r.get("netchange")),
                "change_pct": _num(r.get("pctchange")),
                "volume": _num(r.get("volume")),
                "market_cap": _num(r.get("marketCap")),
                "country": r.get("country"),
                "sector": r.get("sector"),
                "industry": r.get("industry"),
                "exchange": exchange,
            }
        )
    return pd.DataFrame.from_records(
        records,
        columns=["symbol", "name", "price", "change", "change_pct", "volume", "market_cap", "country", "sector", "industry", "exchange"],
    )


class Nasdaq:
    def __init__(self, http: HttpClient):
        self.http = http

    def screener(self, exchanges: list[str], ttl: float = 600) -> pd.DataFrame:
        frames = []
        for ex in exchanges:
            payload = self.http.get_json(
                URL, params={"tableonly": "true", "download": "true", "exchange": ex}, headers=HEADERS, ttl=ttl
            )
            frames.append(parse_screener(payload, ex))
        if not frames:
            return parse_screener({})
        return pd.concat(frames, ignore_index=True).drop_duplicates("symbol")
