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
from .alphavantage import AlphaVantage
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

    def alt_movers(self) -> list[dict[str, Any]]:
        """Extra movers lists (Alpha Vantage). Empty when not configured."""
        return []

    def news_sentiment(self, symbol: str) -> dict[str, Any] | None:
        """Scored news for one symbol (Alpha Vantage). None when not configured."""
        return None


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
        self.alpha = AlphaVantage(
            self.http, cfg.data.alphavantage_api_key, cfg.data.alphavantage_daily_limit, cfg.data.alphavantage_per_minute
        )
        self._splits: dict[str, list[dict]] = {}
        self._quote_api_retry_at = datetime.min.replace(tzinfo=ET)

    # ------------------------------------------------------------------ prices
    def daily_bars(self, symbol: str, days: int = 200) -> pd.DataFrame:
        range_ = "1y" if days > 120 else "6mo" if days > 60 else "3mo"
        end = datetime.now(ET).date()
        start = end - timedelta(days=int(days * 1.5) + 10)
        errors = []
        for name, fetch in self._daily_sources(symbol, range_, start, end, days):
            try:
                df = fetch()
                if len(df):
                    return df.tail(days)
                errors.append(f"{name}: empty")
            except DataError as exc:
                errors.append(f"{name}: {exc}")
        raise DataError(f"no daily bars for {symbol}: " + "; ".join(errors))

    def _daily_sources(self, symbol: str, range_: str, start: date, end: date, days: int):
        yield "yahoo", lambda: self._yahoo_daily(symbol, range_)
        yield "nasdaq", lambda: self.nasdaq.historical(symbol, start, end)
        if self.alpha.enabled:
            yield "alphavantage", lambda: self.alpha.daily(symbol, full=days > 100)
        yield "stooq", lambda: self.stooq.daily(symbol)

    def _yahoo_daily(self, symbol: str, range_: str) -> pd.DataFrame:
        df, _meta, splits = self.yahoo.chart(symbol, range_=range_, interval="1d", ttl=1800)
        self._splits[symbol] = splits
        return df

    def daily_history(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        s = datetime.combine(start, datetime.min.time(), ET)
        e = datetime.combine(end + timedelta(days=1), datetime.min.time(), ET)
        errors = []
        df = None
        sources = [
            ("yahoo", lambda: self.yahoo.chart_period(symbol, s, e)),
            ("nasdaq", lambda: (self.nasdaq.historical(symbol, start, end, ttl=86400), None, [])),
        ]
        if self.alpha.enabled:
            sources.append(("alphavantage", lambda: (self.alpha.daily(symbol, full=True), None, [])))
        sources.append(("stooq", lambda: (self.stooq.daily(symbol), None, [])))
        for name, fetch in sources:
            try:
                df, _meta, splits = fetch()
                if splits:
                    self._splits[symbol] = splits
                break
            except DataError as exc:
                errors.append(f"{name}: {exc}")
        if df is None:
            raise DataError(f"no history for {symbol}: " + "; ".join(errors))
        return df[(df.index.date >= start) & (df.index.date <= end)]

    def splits(self, symbol: str) -> list[dict[str, Any]]:
        return self._splits.get(symbol, [])

    def intraday_bars(self, symbol: str, interval: str = "1m") -> pd.DataFrame:
        df, _meta, _ = self.yahoo.chart(symbol, range_="1d", interval=interval, ttl=20)
        return df

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Yahoo's quote API (has bid/ask), else Yahoo's chart, else Nasdaq,
        else Alpha Vantage (rate-limited, so last)."""
        symbols = sorted(set(symbols))
        out: dict[str, Quote] = {}
        raw: dict[str, dict] = {}
        now = datetime.now(ET)
        if symbols and now >= self._quote_api_retry_at:
            try:
                raw = self.yahoo.quotes(symbols)
            except DataError as exc:
                log.info("yahoo quote API unavailable (%s); falling back for 10 minutes", exc)
                self._quote_api_retry_at = now + timedelta(minutes=10)
        for sym in symbols:
            q = raw.get(sym)
            if q and q.get("regularMarketPrice"):
                out[sym] = self._from_yahoo_quote(sym, q, now)
                continue
            for fetch in (self._from_yahoo_chart, self._from_nasdaq, self._from_alpha):
                try:
                    quote = fetch(sym, now)
                except DataError as exc:
                    log.info("%s failed for %s: %s", fetch.__name__, sym, exc)
                    continue
                if quote is not None:
                    out[sym] = quote
                    break
        return out

    @staticmethod
    def _from_yahoo_quote(sym: str, q: dict[str, Any], now: datetime) -> Quote:
        avg_vol = q.get("averageDailyVolume10Day") or q.get("averageDailyVolume3Month")
        last = float(q["regularMarketPrice"])
        return Quote(
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
            extra={"source": "yahoo", **{k: q.get(k) for k in ("marketCap", "sharesOutstanding", "floatShares", "marketState")}},
        )

    def _from_yahoo_chart(self, sym: str, now: datetime) -> Quote | None:
        df, meta, _ = self.yahoo.chart(sym, range_="1d", interval="1m", ttl=20)
        last = meta.get("regularMarketPrice")
        if not last:
            return None
        return Quote(
            symbol=sym,
            last=float(last),
            ts=datetime.fromtimestamp(meta["regularMarketTime"], ET) if meta.get("regularMarketTime") else now,
            open=float(df["open"].iloc[0]) if len(df) else None,
            day_high=meta.get("regularMarketDayHigh") or (float(df["high"].max()) if len(df) else None),
            day_low=meta.get("regularMarketDayLow") or (float(df["low"].min()) if len(df) else None),
            prev_close=meta.get("chartPreviousClose") or meta.get("previousClose"),
            volume=meta.get("regularMarketVolume") or (float(df["volume"].sum()) if len(df) else None),
            extra={"source": "yahoo_chart"},
        )

    def _from_nasdaq(self, sym: str, now: datetime) -> Quote:
        info = self.nasdaq.info(sym)
        # Nasdaq's quote has no opening price; the strategies then treat the
        # current price as the open, which makes gap setups less likely to fire.
        return Quote(
            symbol=sym, last=info["last"], ts=now, bid=info["bid"], ask=info["ask"],
            day_high=info["day_high"], day_low=info["day_low"], prev_close=info["prev_close"],
            volume=info["volume"], extra={"source": "nasdaq", "market_status": info.get("market_status")},
        )

    def _from_alpha(self, sym: str, now: datetime) -> Quote | None:
        if not self.alpha.enabled:
            return None
        g = self.alpha.global_quote(sym)
        return Quote(
            symbol=sym, last=g["price"], ts=now, open=g["open"], day_high=g["high"], day_low=g["low"],
            prev_close=g["prev_close"], volume=g["volume"], extra={"source": "alphavantage", "day": g.get("day")},
        )

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

    def alt_movers(self) -> list[dict[str, Any]]:
        if not self.alpha.enabled:
            return []
        return self.alpha.movers()

    def news_sentiment(self, symbol: str) -> dict[str, Any] | None:
        if not self.alpha.enabled:
            return None
        return self.alpha.news_sentiment(symbol)

    def stocktwits_trending(self) -> list[dict[str, Any]]:
        return self.social.stocktwits_trending()

    def stocktwits_sentiment(self, symbol: str) -> dict[str, Any]:
        return self.social.stocktwits_sentiment(symbol)

    def reddit_posts(self, sub: str) -> list[dict[str, Any]]:
        return self.social.reddit_hot(sub)


def check_sources(md: LiveMarketData, on_result=None) -> list[tuple[str, str, bool, str]]:
    """Probe each source once: (name, role, ok, detail). Used by `stockagent doctor`.
    Alpha Vantage is probed with the movers list, which the scout then reuses
    from cache, so the check does not waste the free key's 25 daily calls."""
    today = datetime.now(ET).date()
    probes = [
        ("yahoo chart", "prices", lambda: f"{len(md.yahoo.chart('SPY', '5d', '1d', ttl=0)[0])} bars"),
        ("yahoo quote (bid/ask)", "prices", lambda: f"{len(md.yahoo.quotes(['SPY'], ttl=0))} quotes"),
        # Nasdaq's quote API is asked for assetclass=stocks, so probe a stock, not an ETF.
        ("nasdaq daily history", "prices",
         lambda: f"{len(md.nasdaq.historical('AAPL', today - timedelta(days=20), today, ttl=0))} bars"),
        ("nasdaq quote", "prices", lambda: f"last {md.nasdaq.info('AAPL', ttl=0)['last']}"),
        ("stooq daily", "prices", lambda: f"{len(md.stooq.daily('SPY', ttl=0))} bars"),
        ("nasdaq screener", "universe", lambda: f"{len(md.nasdaq.screener(['nasdaq'], ttl=0))} stocks"),
        ("yahoo screeners", "tips", lambda: f"{len(md.yahoo.screener('day_gainers', 25, ttl=0))} rows"),
        ("alphavantage movers", "tips",
         lambda: f"{len(md.alpha.movers())} rows; {md.alpha.remaining_today()} of {md.alpha.daily_limit} calls left today"),
        ("stocktwits trending", "tips", lambda: f"{len(md.social.stocktwits_trending())} symbols"),
        ("reddit r/pennystocks", "tips", lambda: f"{len(md.social.reddit_hot('pennystocks', 10))} posts"),
        ("sec edgar filings", "tips", lambda: f"{len(md.sec.current('8-K', 10))} filings"),
        ("yahoo news", "news", lambda: f"{len(md.yahoo.news('SPY', 5, ttl=0))} items"),
        ("google news rss", "news", lambda: f"{len(md.news_feeds.google('penny stock'))} items"),
    ]
    results = []
    for name, role, fn in probes:
        try:
            res = (name, role, True, fn())
        except Exception as exc:  # noqa: BLE001 - doctor reports every failure
            res = (name, role, False, str(exc)[:150])
        results.append(res)
        if on_result:
            on_result(*res)
    return results
