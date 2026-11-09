import json
import math

import pytest
from conftest import FIXTURES

from stockagent.data import nasdaq, news, sec, social, stooq, yahoo
from stockagent.data.http import DataError


def load(name):
    text = (FIXTURES / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


def test_yahoo_chart_drops_null_rows_and_reads_splits():
    df, meta, splits = yahoo.parse_chart(load("yahoo_chart.json"))
    assert len(df) == 3
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 2.31
    assert all(ts.hour == 0 for ts in df.index)  # normalised to sessions
    assert meta["regularMarketPrice"] == 2.31
    assert splits[0]["numerator"] == 1.0 and splits[0]["denominator"] == 10.0


def test_yahoo_chart_error_raises():
    with pytest.raises(DataError):
        yahoo.parse_chart({"chart": {"result": None, "error": {"code": "Not Found"}}})


def test_yahoo_screener_keeps_equities_only():
    rows = yahoo.parse_screener(load("yahoo_screener.json"))
    assert [r["symbol"] for r in rows] == ["ABCD", "BIGG"]
    assert rows[0]["change_pct"] == 31.2


def test_yahoo_quote_and_news():
    quotes = yahoo.parse_quotes(load("yahoo_quote.json"))
    assert quotes["ABCD"]["bid"] == 2.3 and quotes["ABCD"]["ask"] == 2.32
    items = yahoo.parse_news(load("yahoo_search_news.json"))
    assert items[0]["title"].startswith("Abcd") and items[0]["published"].year == 2025


def test_nasdaq_screener_parses_money_strings():
    df = nasdaq.parse_screener(load("nasdaq_screener.json"), "nasdaq")
    row = df.set_index("symbol").loc["ABCD"]
    assert row["price"] == 2.31 and row["change_pct"] == 10.0 and row["market_cap"] == 45_045_000
    efgh = df.set_index("symbol").loc["EFGH"]
    assert efgh["volume"] == 800_000 and math.isnan(efgh["market_cap"])


def test_stooq_csv():
    df = stooq.parse_csv(load("stooq.csv"))
    assert len(df) == 3 and df["close"].iloc[-1] == 2.31
    with pytest.raises(DataError):
        stooq.parse_csv("No data")


def test_sec_feed_items_and_ticker_map():
    filings = sec.parse_current_feed(load("sec_current_8k.xml"))
    assert len(filings) == 2
    assert filings[0]["form"] == "8-K" and filings[0]["cik"] == 1234567
    assert filings[0]["items"] == ["1.01", "9.01"]
    assert filings[1]["items"] == ["3.01"]
    tickers = sec.parse_ticker_map(load("sec_tickers.json"))
    assert tickers[1234567] == "ABCD"


def test_stocktwits():
    trending = social.parse_stocktwits_trending(load("stocktwits_trending.json"))
    assert [t["symbol"] for t in trending] == ["ABCD", "EFGH"]  # crypto pair dropped
    s = social.parse_stocktwits_stream(load("stocktwits_stream.json"))
    assert s["bullish"] == 2 and s["bearish"] == 1 and s["bull_ratio"] == pytest.approx(2 / 3)


def test_reddit_mentions_skip_stickies_and_stopwords():
    posts = social.parse_reddit_listing(load("reddit_hot.json"))
    assert len(posts) == 2
    ranked = social.rank_reddit_mentions(posts, {"ABCD", "EFGH", "IT", "UP"})
    assert ranked[0]["symbol"] == "ABCD"
    assert "IT" not in {r["symbol"] for r in ranked} and "UP" not in {r["symbol"] for r in ranked}


def test_extract_tickers_requires_universe_for_bare_words():
    assert social.extract_tickers("buying ABCD today") == {}
    assert social.extract_tickers("buying $abcd today") == {"ABCD": 2}
    assert social.extract_tickers("buying ABCD and $ABCD", {"ABCD"}) == {"ABCD": 3}


def test_rss():
    items = news.parse_rss(load("google_news.xml"))
    assert len(items) == 2 and items[0]["publisher"] == "Reuters"
    assert items[0]["published"].day == 5
