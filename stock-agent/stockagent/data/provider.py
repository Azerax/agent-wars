"""One interface over every data source, with fallbacks.

The agent, scout and backtester only talk to a MarketData. Tests swap in a
fake one; nothing else in the code knows where the numbers come from.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from ..clock import ET
from ..config import Config
from ..execution import Quote
from .http import DataError, HttpClient
from .nasdaq import Nasdaq
from .news import News
from .sec import DILUTION_FORMS, SEC
from .social import Social
from .stooq import Stooq
from .yahoo import SCREENERS, Yahoo

log = logging.getLogger(__name__)


class MarketData:
    """Interface. Every method may raise DataError."""

    def daily_bars(self, symbol: str, days: int = 200) -> pd.DataFrame:
        raise NotImplementedError

    def daily_history(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        raise NotImplementedError

    def intraday_bars(self, symbol: str, interval: str = "1m") -> pd.DataFrame:
        raise NotImplementedError

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raise NotImplementedError

    def universe(self) -> pd.DataFrame:
        raise NotImplementedError

    def screener_movers(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def news(self, symbol: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def filings(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def stocktwits_trending(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def stocktwits_sentiment(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    def reddit_posts(self, sub: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def splits(self, symbol: str) -> list[dict[str, Any]]:
        return []


def avg_dollar_volume(df: pd.DataFrame, n: int = 20) -> float | None:
    if df is None or df.empty:
        return None
    tail = df.tail(n)
    return float((tail["close"] * tail["volume"]).mean())


class LiveMarketData(MarketData):
    def __init__(self, cfg: Config, http: HttpClient | None = None):
        self.cfg = cfg
        self.http = http or HttpClient(
            cfg.data.cache_path, cfg.data.user_agent, cfg.data.request_timeout, cfg.data.min_request_interval
        )
        self.yahoo = Yahoo(self.http)
        self.nasdaq = Nasdaq(self.http)
        self.stooq = Stooq(self.http)
        self.sec = SEC(self.http, cfg.data.sec_user_agent)
        self.social = Social(self.http)
        self.news_feeds = News(self.http)
        self._splits: dict[str, list[dict]] = {}
        self._quote_api_ok = True

    # ------------------------------------------------------------------ prices
    def daily_bars(self, symbol: str, days: int = 200) -> pd.DataFrame:
        range_ = "1y" if days > 120 else "6mo" if days > 60 else "3mo"
        try:
            df, _meta, splits = self.yahoo.chart(symbol, range_=range_, interval="1d", ttl=1800)
            self._splits[symbol] = splits
        except DataError as exc:
            log.info("yahoo daily failed for %s (%s); trying stooq", symbol, exc)
            df = self.stooq.daily(symbol)
        return df.tail(days)

    def daily_history(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        s = datetime.combine(start, datetime.min.time(), ET)
        e = datetime.combine(end + timedelta(days=1), datetime.min.time(), ET)
        try:
            df, _meta, splits = self.yahoo.chart_period(symbol, s, e)
            self._splits[symbol] = splits
        except DataError:
            df = self.stooq.daily(symbol)
        return df[(df.index.date >= start) & (df.index.date <= end)]

    def splits(self, symbol: str) -> list[dict[str, Any]]:
        return self._splits.get(symbol, [])

    def intraday_bars(self, symbol: str, interval: str = "1m") -> pd.DataFrame:
        df, _meta, _ = self.yahoo.chart(symbol, range_="1d", interval=interval, ttl=20)
        return df

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        symbols = sorted(set(symbols))
        out: dict[str, Quote] = {}
        raw: dict[str, dict] = {}
        if self._quote_api_ok and symbols:
            try:
                raw = self.yahoo.quotes(symbols)
            except DataError as exc:
                log.info("yahoo quote API unavailable (%s); using chart meta", exc)
                self._quote_api_ok = False
        now = datetime.now(ET)
        for sym in symbols:
            q = raw.get(sym)
            if q and q.get("regularMarketPrice"):
                avg_vol = q.get("averageDailyVolume10Day") or q.get("averageDailyVolume3Month")
                last = float(q["regularMarketPrice"])
                out[sym] = Quote(
                    symbol=sym,
                    last=last,
                    ts=datetime.fromtimestamp(q["regularMarketTime"], ET) if q.get("regularMarketTime") else now,
                    bid=q.get("bid") or None,
                    ask=q.get("ask") or None,
                    open=q.get("regularMarketOpen"),
                    day_high=q.get("regularMarketDayHigh"),
                    day_low=q.get("regularMarketDayLow"),
                    prev_close=q.get("regularMarketPreviousClose"),
                    volume=q.get("regularMarketVolume"),
                    avg_volume=avg_vol,
                    avg_dollar_volume=(avg_vol * last) if avg_vol else None,
                    extra={k: q.get(k) for k in ("marketCap", "sharesOutstanding", "floatShares", "marketState")},
                )
                continue
            try:
                df, meta, _ = self.yahoo.chart(sym, range_="1d", interval="1m", ttl=20)
            except DataError as exc:
                log.info("no quote for %s: %s", sym, exc)
                continue
            last = meta.get("regularMarketPrice")
            if not last:
                continue
            out[sym] = Quote(
                symbol=sym,
                last=float(last),
                ts=datetime.fromtimestamp(meta["regularMarketTime"], ET) if meta.get("regularMarketTime") else now,
                open=float(df["open"].iloc[0]) if len(df) else None,
                day_high=meta.get("regularMarketDayHigh") or (float(df["high"].max()) if len(df) else None),
                day_low=meta.get("regularMarketDayLow") or (float(df["low"].min()) if len(df) else None),
                prev_close=meta.get("chartPreviousClose") or meta.get("previousClose"),
                volume=meta.get("regularMarketVolume") or (float(df["volume"].sum()) if len(df) else None),
            )
        return out

    # ---------------------------------------------------------------- discovery
    def universe(self) -> pd.DataFrame:
        return self.nasdaq.screener(self.cfg.universe.exchanges)

    def screener_movers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        errors = []
        for scr in SCREENERS:
            try:
                for row in self.yahoo.screener(scr, count=100):
                    row["screener"] = scr
                    out.append(row)
            except DataError as exc:
                errors.append(f"{scr}: {exc}")
        if not out and errors:
            raise DataError("; ".join(errors))
        return out

    def news(self, symbol: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        errors = []
        for fn in (lambda: self.yahoo.news(symbol), lambda: self.news_feeds.yahoo_rss(symbol),
                   lambda: self.news_feeds.google(f'"{symbol}" stock')):
            try:
                items.extend(fn())
            except DataError as exc:
                errors.append(str(exc))
        if not items and errors:
            raise DataError("; ".join(errors))
        seen = set()
        unique = []
        for it in sorted(items, key=lambda i: i.get("published") or datetime.min.replace(tzinfo=ET), reverse=True):
            key = (it.get("title") or "").lower()[:90]
            if key and key not in seen:
                seen.add(key)
                unique.append(it)
        return unique[:25]

    def filings(self) -> list[dict[str, Any]]:
        out = self.sec.current("8-K", 100)
        for form in ("424B4", "424B5", "S-1", "S-3"):
            try:
                out.extend(self.sec.current(form, 40))
            except DataError:
                continue
        for f in out:
            f["dilution"] = f["form"] in DILUTION_FORMS
        return out

    def stocktwits_trending(self) -> list[dict[str, Any]]:
        return self.social.stocktwits_trending()

    def stocktwits_sentiment(self, symbol: str) -> dict[str, Any]:
        return self.social.stocktwits_sentiment(symbol)

    def reddit_posts(self, sub: str) -> list[dict[str, Any]]:
        return self.social.reddit_hot(sub)


def check_sources(md: LiveMarketData, on_result=None) -> list[tuple[str, bool, str]]:
    """Probe each source once. Used by `stockagent doctor`."""
    probes = [
        ("yahoo chart (prices)", lambda: f"{len(md.yahoo.chart('SPY', '5d', '1d', ttl=0)[0])} bars"),
        ("yahoo quote (bid/ask)", lambda: f"{len(md.yahoo.quotes(['SPY'], ttl=0))} quotes"),
        ("yahoo screeners (movers)", lambda: f"{len(md.yahoo.screener('day_gainers', 25, ttl=0))} rows"),
        ("yahoo news", lambda: f"{len(md.yahoo.news('SPY', 5, ttl=0))} items"),
        ("nasdaq screener (universe)", lambda: f"{len(md.nasdaq.screener(['nasdaq'], ttl=0))} stocks"),
        ("stooq (fallback prices)", lambda: f"{len(md.stooq.daily('SPY', ttl=0))} bars"),
        ("stocktwits trending", lambda: f"{len(md.social.stocktwits_trending())} symbols"),
        ("reddit r/pennystocks", lambda: f"{len(md.social.reddit_hot('pennystocks', 10))} posts"),
        ("google news rss", lambda: f"{len(md.news_feeds.google('penny stock'))} items"),
        ("sec edgar filings", lambda: f"{len(md.sec.current('8-K', 10))} filings"),
    ]
    results = []
    for name, fn in probes:
        try:
            res = (name, True, fn())
        except Exception as exc:  # noqa: BLE001 - doctor reports every failure
            res = (name, False, str(exc)[:160])
        results.append(res)
        if on_result:
            on_result(*res)
    return results
