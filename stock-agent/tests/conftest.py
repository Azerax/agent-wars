from __future__ import annotations

import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stockagent import clock as mcal  # noqa: E402
from stockagent.broker import SimBroker  # noqa: E402
from stockagent.config import Config  # noqa: E402
from stockagent.data.http import DataError  # noqa: E402
from stockagent.data.provider import MarketData  # noqa: E402
from stockagent.db import Database  # noqa: E402
from stockagent.execution import Quote  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), mcal.ET)


class FakeMarketData(MarketData):
    """Scripted market for tests. Quotes are set by the test as time moves."""

    def __init__(self, bars: dict[str, pd.DataFrame] | None = None):
        self.bars = bars or {}
        self.live: dict[str, Quote] = {}
        self.universe_rows: list[dict[str, Any]] = []
        self.movers: list[dict[str, Any]] = []
        self.headlines: dict[str, list[dict[str, Any]]] = {}
        self.filing_list: list[dict[str, Any]] = []
        self.trending: list[dict[str, Any]] = []
        self.posts: dict[str, list[dict[str, Any]]] = {}
        self.fail: set[str] = set()

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise DataError(f"{name} down")

    def daily_bars(self, symbol, days=200):
        self._check("daily")
        if symbol not in self.bars:
            raise DataError(f"no bars for {symbol}")
        return self.bars[symbol].tail(days)

    def daily_history(self, symbol, start, end):
        df = self.daily_bars(symbol, 10_000)
        return df[(df.index.date >= start) & (df.index.date <= end)]

    def intraday_bars(self, symbol, interval="1m"):
        raise DataError("no intraday in fake")

    def quotes(self, symbols):
        self._check("quotes")
        return {s: Quote(**{**self.live[s].__dict__}) for s in symbols if s in self.live}

    def universe(self):
        self._check("universe")
        return pd.DataFrame(self.universe_rows)

    def screener_movers(self):
        self._check("movers")
        return list(self.movers)

    def news(self, symbol):
        self._check("news")
        return list(self.headlines.get(symbol, []))

    def filings(self):
        self._check("filings")
        return list(self.filing_list)

    def stocktwits_trending(self):
        self._check("stocktwits")
        return list(self.trending)

    def stocktwits_sentiment(self, symbol):
        return {"bull_ratio": 0.7, "messages": 30}

    def reddit_posts(self, sub):
        self._check("reddit")
        return list(self.posts.get(sub, []))


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.data.reddit_subs = ["pennystocks"]
    return c


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def monday() -> date:
    return date(2026, 11, 2)


@pytest.fixture
def clk(monday) -> mcal.SimClock:
    return mcal.SimClock(at(monday, 10, 0))


@pytest.fixture
def broker(db, cfg, clk) -> SimBroker:
    b = SimBroker(db, cfg, clk)
    b.open_account(100.0)
    return b
