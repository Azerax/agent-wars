"""Nasdaq's stock screener: every listed US common stock with last price,
change and volume in one request. This is the agent's universe."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pandas as pd

from ..clock import ET
from .http import DataError, HttpClient

URL = "https://api.nasdaq.com/api/screener/stocks"
HISTORICAL_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
INFO_URL = "https://api.nasdaq.com/api/quote/{symbol}/info"
HEADERS = {"Accept": "application/json, text/plain, */*"}


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


def _status_ok(payload: dict[str, Any]) -> None:
    status = payload.get("status") or {}
    if status.get("rCode") not in (None, 200):
        msgs = status.get("bCodeMessage") or status.get("developerMessage")
        raise DataError(f"nasdaq: rCode {status.get('rCode')} {msgs}")


def parse_historical(payload: dict[str, Any]) -> pd.DataFrame:
    """Daily bars from /api/quote/<sym>/historical, oldest first."""
    _status_ok(payload)
    data = payload.get("data") or {}
    rows = ((data.get("tradesTable") or {}).get("rows")) or []
    records = []
    for r in rows:
        try:
            day = datetime.strptime(r["date"], "%m/%d/%Y").date()
        except (KeyError, ValueError):
            continue
        rec = {k: _num(r.get(k)) for k in ("open", "high", "low", "close", "volume")}
        if rec["close"] is None:
            continue
        for k in ("open", "high", "low"):
            if rec[k] is None:
                rec[k] = rec["close"]
        rec["volume"] = rec["volume"] or 0.0
        records.append((day, rec))
    if not records:
        raise DataError("nasdaq historical: no rows")
    records.sort(key=lambda x: x[0])
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=ET) for d, _ in records])
    df = pd.DataFrame([r for _, r in records], index=idx)[["open", "high", "low", "close", "volume"]].astype(float)
    return df[~df.index.duplicated(keep="last")]


def parse_info(payload: dict[str, Any]) -> dict[str, Any]:
    """Last price, change, bid/ask and day range from /api/quote/<sym>/info."""
    _status_ok(payload)
    data = payload.get("data") or {}
    prim = data.get("primaryData") or {}
    last = _num(prim.get("lastSalePrice"))
    if last is None:
        raise DataError("nasdaq info: no last sale price")
    change = _num(prim.get("netChange"))
    day_low = day_high = None
    rng = ((data.get("keyStats") or {}).get("dayrange") or {}).get("value") or ""
    parts = [_num(x) for x in re.split(r"\s*[-/]\s*", rng.replace("$", "")) if x.strip()]
    if len(parts) == 2 and all(p is not None for p in parts):
        day_low, day_high = min(parts), max(parts)
    return {
        "symbol": (data.get("symbol") or "").upper(),
        "last": last,
        "prev_close": (last - change) if change is not None else None,
        "bid": _num(prim.get("bidPrice")),
        "ask": _num(prim.get("askPrice")),
        "volume": _num(prim.get("volume")),
        "day_low": day_low,
        "day_high": day_high,
        "market_status": data.get("marketStatus"),
        "timestamp": prim.get("lastTradeTimestamp"),
    }


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

    def historical(self, symbol: str, start: date, end: date, ttl: float = 1800) -> pd.DataFrame:
        payload = self.http.get_json(
            HISTORICAL_URL.format(symbol=symbol.upper()),
            params={"assetclass": "stocks", "fromdate": start.isoformat(), "todate": end.isoformat(), "limit": 9999},
            headers=HEADERS, ttl=ttl,
        )
        return parse_historical(payload)

    def info(self, symbol: str, ttl: float = 15) -> dict[str, Any]:
        payload = self.http.get_json(
            INFO_URL.format(symbol=symbol.upper()), params={"assetclass": "stocks"}, headers=HEADERS, ttl=ttl
        )
        return parse_info(payload)
