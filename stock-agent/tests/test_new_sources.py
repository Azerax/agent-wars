"""Nasdaq price fallbacks, Alpha Vantage, the API-key file, Reddit RSS."""
import json
from datetime import date

import pytest
from conftest import FIXTURES
from test_scout_learning import scripted_market

from stockagent.config import Config, find_alphavantage_key, load_config, parse_key_file
from stockagent.data import alphavantage as av
from stockagent.data import nasdaq, social
from stockagent.data.http import DataError, HttpClient, cache_key, redact
from stockagent.data.provider import LiveMarketData
from stockagent.scout import Scout


def load(name):
    text = (FIXTURES / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


# --------------------------------------------------------------------- nasdaq
def test_nasdaq_historical_sorts_and_fills_missing_prices():
    df = nasdaq.parse_historical(load("nasdaq_historical.json"))
    assert list(df.index.date) == [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
    assert df["close"].iloc[-1] == 2.31 and df["volume"].iloc[-1] == 5_400_000
    assert df.loc[df.index[0], "open"] == 2.05 and df["volume"].iloc[0] == 0  # N/A filled from close / zero


def test_nasdaq_info_quote():
    q = nasdaq.parse_info(load("nasdaq_info.json"))
    assert q["last"] == 2.31 and q["bid"] == 2.30 and q["ask"] == 2.32
    assert q["prev_close"] == pytest.approx(2.10)
    assert (q["day_low"], q["day_high"]) == (2.05, 2.40)


def test_nasdaq_error_status_raises():
    with pytest.raises(DataError, match="400"):
        nasdaq.parse_info(load("nasdaq_error.json"))


# --------------------------------------------------------------- alphavantage
def test_av_parsers():
    g = av.parse_global_quote(load("av_global_quote.json"))
    assert g["price"] == 2.31 and g["open"] == 2.12 and g["prev_close"] == 2.10 and g["change_pct"] == 10.0
    df = av.parse_daily(load("av_daily.json"))
    assert list(df.index.date) == [date(2026, 11, 5), date(2026, 11, 6)] and df["close"].iloc[-1] == 2.31
    movers = av.parse_movers(load("av_movers.json"))
    assert {(m["symbol"], m["list"]) for m in movers} >= {("ABCD", "gainers"), ("MNOP", "active"), ("EFGH", "losers")}
    news = av.parse_news_sentiment(load("av_news.json"), "ABCD")
    assert news["articles"] == 2
    assert news["sentiment"] == pytest.approx((0.9 * 0.4 + 0.1 * -0.2) / 1.0)
    assert news["headlines"][0]["published"].hour == 8


def test_av_rate_limit_message_is_an_error():
    with pytest.raises(DataError, match="limit"):
        av.check_payload(load("av_limit.json"))


class _StubHttp(HttpClient):
    """Counts real calls; serves canned payloads."""

    def __init__(self, payloads):
        super().__init__(":memory:", "test", min_interval=0.0)
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, headers=None, ttl=0):
        self.calls.append(dict(params or {}))
        return self.payloads.pop(0)


def test_av_budget_cache_and_exhaustion():
    http = _StubHttp([load("av_movers.json"), load("av_limit.json")])
    client = av.AlphaVantage(http, "SECRETKEY123", daily_limit=25, per_minute=5)
    client.movers()
    client.movers()  # second call served from cache, no request spent
    assert len(http.calls) == 1 and client.remaining_today() == 24
    with pytest.raises(DataError, match="limit"):
        client.global_quote("ABCD")
    assert client.remaining_today() == 0  # Alpha Vantage said the day's quota is gone


def test_av_daily_budget_is_enforced_before_calling():
    http = _StubHttp([load("av_global_quote.json")] * 5)
    client = av.AlphaVantage(http, "SECRETKEY123", daily_limit=2, per_minute=10)
    client.global_quote("AAAA")
    client.global_quote("BBBB")
    with pytest.raises(DataError, match="budget"):
        client.global_quote("CCCC")
    assert len(http.calls) == 2


def test_api_keys_never_reach_cache_keys_or_errors():
    assert "SECRET" not in cache_key("https://www.alphavantage.co/query", {"function": "X", "apikey": "SECRET"})
    assert redact("GET /query?function=X&apikey=SECRET123&x=1 failed") == "GET /query?function=X&apikey=***&x=1 failed"


def test_av_disabled_without_key():
    client = av.AlphaVantage(_StubHttp([]), "")
    assert not client.enabled
    with pytest.raises(DataError, match="ALPHA"):
        client.movers()


# ------------------------------------------------------------------ key file
@pytest.mark.parametrize("text", [
    "ALPHA=ABC123XYZ9", '$env:ALPHA = "ABC123XYZ9"', "set ALPHA=ABC123XYZ9", "export ALPHA='ABC123XYZ9'",
    "ABC123XYZ9", "# my key\nALPHA = ABC123XYZ9\n",
])
def test_key_file_formats(text):
    assert parse_key_file(text) == "ABC123XYZ9"


def test_key_found_in_dot_alpha_file_even_utf16(tmp_path, monkeypatch):
    for name in ("ALPHA", "ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".alpha").write_text("ALPHA=FILEKEY12345\r\n", encoding="utf-16")
    key, where = find_alphavantage_key()
    assert key == "FILEKEY12345" and ".alpha" in where
    monkeypatch.setenv("ALPHA", "ENVKEY12345")
    assert find_alphavantage_key()[0] == "ENVKEY12345"  # environment wins
    assert load_config().data.alphavantage_api_key == "ENVKEY12345"


# ---------------------------------------------------------------- reddit rss
def test_reddit_rss_posts_feed_ticker_extraction():
    posts = social.parse_reddit_rss(load("reddit_hot.rss"))
    assert posts[0]["title"] == "ABCD DD thread" and "$ABCD" in posts[0]["text"]
    assert social.rank_reddit_mentions(posts, {"ABCD"})[0]["symbol"] == "ABCD"


# ---------------------------------------------------------- provider fallback
def test_daily_bars_fall_back_to_nasdaq_when_yahoo_blocks(monkeypatch):
    md = LiveMarketData(Config(), HttpClient(None, "test", min_interval=0.0))

    def blocked(*a, **kw):
        raise DataError("query1.finance.yahoo.com returned HTTP 429")

    monkeypatch.setattr(md.yahoo, "chart", blocked)
    monkeypatch.setattr(md.nasdaq, "historical", lambda *a, **kw: nasdaq.parse_historical(load("nasdaq_historical.json")))
    df = md.daily_bars("ABCD", 50)
    assert df["close"].iloc[-1] == 2.31

    monkeypatch.setattr(md.yahoo, "quotes", blocked)
    monkeypatch.setattr(md.nasdaq, "info", lambda *a, **kw: nasdaq.parse_info(load("nasdaq_info.json")))
    q = md.quotes(["ABCD"])["ABCD"]
    assert q.last == 2.31 and q.bid == 2.30 and q.extra["source"] == "nasdaq"


# --------------------------------------------------------------------- scout
def test_scout_uses_alpha_vantage_movers_and_sentiment(cfg, db, clk):
    md = scripted_market()
    md.alt_movers = lambda: av.parse_movers(load("av_movers.json"))
    md.news_sentiment = lambda sym: av.parse_news_sentiment(load("av_news.json"), sym)
    cands = Scout(cfg, db, md, clk).run()
    sources = {r["source"] for r in db.all("SELECT source FROM tips")}
    assert "alphavantage:gainers" in sources and "alphavantage:losers" not in sources
    top = next(c for c in cands if c["symbol"] == "ABCD")
    assert "av_sentiment" in top["catalyst"].tags and top["news_sentiment"]["articles"] == 2
