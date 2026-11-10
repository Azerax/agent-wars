"""Alpaca Market Data API: a licensed source for prices, bars, movers and news.

A free Alpaca account (paper trading, no deposit) comes with API keys whose
terms allow exactly this kind of program. Free ("Basic") plan, per Alpaca:
- 200 requests per minute;
- real-time data from the IEX exchange only (latest trade, bid/ask);
- consolidated (SIP) history for everything older than 15 minutes.

IEX carries only a few percent of US volume, so volume and liquidity come
from SIP bars (15 minutes delayed), while price and bid/ask come from IEX.

Keys: ALPACA_KEY / ALPACA_SECRET (or APCA_API_KEY_ID / APCA_API_SECRET_KEY)
environment variables, or the same lines in a gitignored .alpaca file.
Endpoints and field names follow Alpaca's official SDK (alpaca-py).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .. import clock as mcal
from ..clock import ET
from .http import DataError, HttpClient

DATA = "https://data.alpaca.markets"
TRADING = "https://paper-api.alpaca.markets"
EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}  # ARCA and BATS list mostly ETFs; OTC is out of scope
SNAPSHOT_BATCH = 200
SIP_DELAY = timedelta(minutes=16)  # free plan: SIP data must be at least 15 minutes old


def parse_ts(text: str | None) -> datetime | None:
    """RFC 3339 with up to nanosecond precision, e.g. 2026-11-09T14:30:00.123456789Z."""
    if not text:
        return None
    t = text.replace("Z", "+00:00")
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", t)
    if m:
        head, frac, tz = m.groups()
        t = head + (f".{frac[:6].ljust(6, '0')}" if frac else "") + tz
    return datetime.fromisoformat(t)


def check(payload: Any) -> Any:
    if isinstance(payload, dict) and "message" in payload and len(payload) <= 2 and "code" in payload:
        raise DataError(f"alpaca: {payload.get('message')}")
    return payload


def parse_assets(payload: Any) -> pd.DataFrame:
    rows = []
    for a in check(payload) or []:
        if a.get("status") != "active" or not a.get("tradable"):
            continue
        if a.get("class", "us_equity") != "us_equity" or a.get("exchange") not in EXCHANGES:
            continue
        rows.append({"symbol": a["symbol"].upper(), "name": a.get("name"), "exchange": a.get("exchange").lower()})
    df = pd.DataFrame(rows, columns=["symbol", "name", "exchange"])
    for col in ("price", "change", "change_pct", "volume", "market_cap", "country", "sector", "industry"):
        df[col] = None
    return df


def parse_bars(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Multi-symbol bars ({"bars": {sym: [...]}}) or single ({"bars": [...], "symbol": sym})."""
    p = check(payload) or {}
    bars = p.get("bars") or {}
    if isinstance(bars, list):
        return {p.get("symbol", ""): bars}
    return {k: v or [] for k, v in bars.items()}


def bars_frame(rows: list[dict[str, Any]], daily: bool) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    idx = [parse_ts(r["t"]).astimezone(ET) for r in rows]
    df = pd.DataFrame(
        {"open": [r["o"] for r in rows], "high": [r["h"] for r in rows], "low": [r["l"] for r in rows],
         "close": [r["c"] for r in rows], "volume": [r.get("v", 0) for r in rows]},
        index=pd.DatetimeIndex(idx),
    ).astype(float)
    if daily:
        df.index = pd.DatetimeIndex([d.normalize() for d in df.index])
        df = df[~df.index.duplicated(keep="last")]
    return df.sort_index()


def parse_snapshots(payload: Any) -> dict[str, dict[str, Any]]:
    p = check(payload) or {}
    if "snapshots" in p and isinstance(p["snapshots"], dict):
        p = p["snapshots"]
    out = {}
    for sym, s in p.items():
        if not isinstance(s, dict):
            continue
        trade = s.get("latestTrade") or {}
        quote = s.get("latestQuote") or {}
        day = s.get("dailyBar") or {}
        prev = s.get("prevDailyBar") or {}
        minute = s.get("minuteBar") or {}
        last = trade.get("p") or minute.get("c") or day.get("c")
        if not last:
            continue
        out[sym.upper()] = {
            "last": float(last),
            "ts": parse_ts(trade.get("t") or minute.get("t") or day.get("t")),
            "bid": quote.get("bp") or None,
            "ask": quote.get("ap") or None,
            "open": day.get("o"), "high": day.get("h"), "low": day.get("l"), "iex_volume": day.get("v"),
            "prev_close": prev.get("c"),
        }
    return out


def parse_movers(payload: Any) -> list[dict[str, Any]]:
    p = check(payload) or {}
    out = []
    for key, kind in (("gainers", "gainers"), ("losers", "losers")):
        for m in p.get(key) or []:
            out.append({"symbol": m["symbol"].upper(), "list": kind, "price": m.get("price"),
                        "change_pct": m.get("percent_change"), "volume": None})
    return out


def parse_most_actives(payload: Any) -> list[dict[str, Any]]:
    p = check(payload) or {}
    return [{"symbol": m["symbol"].upper(), "list": "active", "price": None, "change_pct": None,
             "volume": m.get("volume")} for m in p.get("most_actives") or []]


def parse_news(payload: Any) -> list[dict[str, Any]]:
    p = check(payload) or {}
    return [
        {"title": n.get("headline", ""), "url": n.get("url"), "publisher": n.get("source"),
         "published": parse_ts(n.get("created_at")), "tickers": n.get("symbols") or []}
        for n in p.get("news") or []
    ]


def aggregate_today(df: pd.DataFrame) -> dict[str, float] | None:
    """Open/high/low/volume so far today from intraday SIP bars."""
    if df is None or df.empty:
        return None
    return {"open": float(df["open"].iloc[0]), "high": float(df["high"].max()), "low": float(df["low"].min()),
            "volume": float(df["volume"].sum()), "close": float(df["close"].iloc[-1])}


class Alpaca:
    def __init__(self, http: HttpClient, key: str, secret: str):
        self.http = http
        self.key = key or ""
        self.secret = secret or ""

    @property
    def enabled(self) -> bool:
        return bool(self.key and self.secret)

    def _get(self, base: str, path: str, params: dict[str, Any] | None, ttl: float) -> Any:
        if not self.enabled:
            raise DataError("alpaca: no keys (put ALPACA_KEY=... and ALPACA_SECRET=... in a .alpaca file)")
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret, "Accept": "application/json"}
        return check(self.http.get_json(base + path, params=params, headers=headers, ttl=ttl))

    def assets(self, ttl: float = 86400) -> pd.DataFrame:
        return parse_assets(self._get(TRADING, "/v2/assets", {"status": "active", "asset_class": "us_equity"}, ttl))

    def bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime, feed: str = "sip",
             ttl: float = 900) -> dict[str, pd.DataFrame]:
        if feed == "sip":
            end = min(end, datetime.now(timezone.utc) - SIP_DELAY)
        if end <= start:
            return {s: bars_frame([], timeframe == "1Day") for s in symbols}
        rows: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        params: dict[str, Any] = {
            "symbols": ",".join(symbols), "timeframe": timeframe, "feed": feed, "adjustment": "split",
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "limit": 10000, "sort": "asc",
        }
        for _ in range(50):  # pages
            payload = self._get(DATA, "/v2/stocks/bars", params, ttl)
            for sym, bars in parse_bars(payload).items():
                rows.setdefault(sym, []).extend(bars)
            token = payload.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        return {s: bars_frame(r, timeframe == "1Day") for s, r in rows.items()}

    def snapshots(self, symbols: list[str], feed: str = "iex", ttl: float = 10) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(symbols), SNAPSHOT_BATCH):
            chunk = symbols[i : i + SNAPSHOT_BATCH]
            out.update(parse_snapshots(self._get(DATA, "/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": feed}, ttl)))
        return out

    def movers(self, top: int = 50, ttl: float = 300) -> list[dict[str, Any]]:
        return parse_movers(self._get(DATA, "/v1beta1/screener/stocks/movers", {"top": top}, ttl))

    def most_actives(self, top: int = 50, ttl: float = 300) -> list[dict[str, Any]]:
        return parse_most_actives(
            self._get(DATA, "/v1beta1/screener/stocks/most-actives", {"top": top, "by": "volume"}, ttl)
        )

    def news(self, symbol: str, limit: int = 20, ttl: float = 900) -> list[dict[str, Any]]:
        return parse_news(self._get(DATA, "/v1beta1/news", {"symbols": symbol, "limit": limit, "sort": "desc"}, ttl))

    def today_sip(self, symbols: list[str], now: datetime | None = None, ttl: float = 300) -> dict[str, dict[str, float]]:
        """Today's open/high/low/volume from consolidated 15-minute bars, as of
        about 15 minutes ago (the free plan's delay)."""
        now = now or datetime.now(ET)
        today = now.astimezone(ET).date()
        if not mcal.is_trading_day(today) or not symbols:
            return {}
        open_dt, _ = mcal.session_times(today)
        frames = self.bars(symbols, "15Min", open_dt, now, feed="sip", ttl=ttl)
        out = {}
        for sym, df in frames.items():
            agg = aggregate_today(df)
            if agg:
                out[sym] = agg
        return out

    def previous_day(self, symbols: list[str], today: date) -> dict[str, dict[str, float]]:
        """Last completed session's close and full volume for each symbol."""
        prev = mcal.prev_trading_day(today)
        start = datetime.combine(prev, datetime.min.time(), ET)
        end = datetime.combine(prev, datetime.max.time(), ET)
        out = {}
        for i in range(0, len(symbols), SNAPSHOT_BATCH):
            for sym, df in self.bars(symbols[i : i + SNAPSHOT_BATCH], "1Day", start, end, ttl=6 * 3600).items():
                if len(df):
                    out[sym] = {"close": float(df["close"].iloc[-1]), "volume": float(df["volume"].iloc[-1])}
        return out
