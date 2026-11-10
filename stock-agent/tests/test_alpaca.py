"""Alpaca: parsers, the client (paging, SIP delay, batching, auth headers),
the provider, the key file, and that Alpaca mode never touches Yahoo/Nasdaq."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import FIXTURES
from test_scout_learning import scripted_market

from stockagent.config import Config, find_alpaca_keys, load_config, parse_alpaca_file
from stockagent.data import alpaca as ap
from stockagent.data.http import DataError, HttpClient
from stockagent.data.provider import AlpacaMarketData, LiveMarketData, make_market_data
from stockagent.scout import Scout


def load(name):
    return json.loads((FIXTURES / name).read_text())


class Recorder(HttpClient):
    """Serves fixtures by path and records every request."""

    def __init__(self, routes):
        super().__init__(None, "test", min_interval=0.0)
        self.routes = routes  # path suffix -> payload or list of payloads (consumed in order)
        self.calls = []

    def get_json(self, url, params=None, headers=None, ttl=0):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "__seq__" in payload[0]:
                    return payload.pop(0)["body"]
                return payload
        raise DataError(f"no route for {url}")


def seq(*names):
    return [{"__seq__": True, "body": load(n)} for n in names]


# -------------------------------------------------------------------- parsers
def test_timestamps_with_nanoseconds():
    t = ap.parse_ts("2026-11-09T15:12:03.123456789Z")
    assert t == datetime(2026, 11, 9, 15, 12, 3, 123456, tzinfo=timezone.utc)


def test_assets_keep_listed_tradable_common_stock_exchanges():
    df = ap.parse_assets(load("alpaca_assets.json"))
    assert sorted(df["symbol"]) == ["ABCD", "EFGH"]  # no ETF exchange, no OTC, no inactive


def test_snapshots():
    snaps = ap.parse_snapshots(load("alpaca_snapshots.json"))
    s = snaps["ABCD"]
    assert s["last"] == 2.36 and s["bid"] == 2.35 and s["ask"] == 2.37 and s["prev_close"] == 2.31
    assert "NOPE" not in snaps


def test_movers_most_actives_news():
    movers = ap.parse_movers(load("alpaca_movers.json"))
    assert {(m["symbol"], m["list"]) for m in movers} == {("ABCD", "gainers"), ("WXYZ", "gainers"), ("EFGH", "losers")}
    actives = ap.parse_most_actives(load("alpaca_most_actives.json"))
    assert actives[0] == {"symbol": "MNOP", "list": "active", "price": None, "change_pct": None, "volume": 95000000}
    news = ap.parse_news(load("alpaca_news.json"))
    assert news[0]["title"].startswith("Abcd") and news[0]["publisher"] == "benzinga" and news[0]["published"].hour == 13


def test_error_payload_raises():
    with pytest.raises(DataError, match="not authorized"):
        ap.check(load("alpaca_error.json"))


# --------------------------------------------------------------------- client
def test_bars_follow_pages_and_send_auth_headers():
    http = Recorder({"/v2/stocks/bars": seq("alpaca_bars_page1.json", "alpaca_bars_page2.json")})
    client = ap.Alpaca(http, "PKTESTKEY123", "secret123")
    now = datetime.now(timezone.utc)
    frames = client.bars(["ABCD", "EFGH"], "1Day", now - timedelta(days=30), now)
    assert list(frames["ABCD"]["close"]) == [2.1, 2.31] and len(frames["EFGH"]) == 1
    assert len(http.calls) == 2 and http.calls[1]["params"]["page_token"] == "QUJDRHwyMDI2"
    assert http.calls[0]["headers"]["APCA-API-KEY-ID"] == "PKTESTKEY123"
    assert "secret123" not in json.dumps([c["params"] for c in http.calls])  # secrets only in headers
    assert http.calls[0]["params"]["feed"] == "sip"


def test_sip_requests_stop_16_minutes_ago():
    http = Recorder({"/v2/stocks/bars": {"bars": {}, "next_page_token": None}})
    client = ap.Alpaca(http, "k", "s")
    now = datetime.now(timezone.utc)
    client.bars(["ABCD"], "15Min", now - timedelta(hours=2), now)
    end = ap.parse_ts(http.calls[0]["params"]["end"])
    assert now - end >= timedelta(minutes=15)


def test_snapshots_are_batched():
    http = Recorder({"/v2/stocks/snapshots": {}})
    ap.Alpaca(http, "k", "s").snapshots([f"S{i}" for i in range(450)])
    assert len(http.calls) == 3


def test_no_keys_no_requests():
    http = Recorder({})
    with pytest.raises(DataError, match="alpaca"):
        ap.Alpaca(http, "", "").movers()
    assert http.calls == []


# ------------------------------------------------------------------- provider
def alpaca_md(routes):
    cfg = Config()
    cfg.data.alpaca_key, cfg.data.alpaca_secret = "k", "s"
    http = Recorder(routes)
    return make_market_data(cfg, http), http


def test_quotes_take_price_from_iex_and_volume_from_sip(monkeypatch):
    md, http = alpaca_md({"/v2/stocks/snapshots": load("alpaca_snapshots.json"),
                          "/v2/stocks/bars": load("alpaca_bars_15min.json")})
    assert isinstance(md, AlpacaMarketData)
    monkeypatch.setattr(ap.mcal, "is_trading_day", lambda d: True)
    q = md.quotes(["ABCD"])["ABCD"]
    assert q.last == 2.36 and q.bid == 2.35 and q.ask == 2.37
    assert q.volume == 2_500_000  # consolidated, not the 61,000 IEX printed
    assert q.open == 2.12 and q.day_high == 2.4
    assert not any("yahoo" in c["url"] or "nasdaq" in c["url"] for c in http.calls)


def test_screener_movers_are_named_and_skip_losers():
    md, _ = alpaca_md({"/movers": load("alpaca_movers.json"), "/most-actives": load("alpaca_most_actives.json")})
    rows = md.screener_movers()
    assert {r["source"] for r in rows} == {"alpaca:gainers", "alpaca:active"}
    assert "EFGH" not in {r["symbol"] for r in rows}


def test_scout_prices_chatter_tips_through_alpaca(cfg, db, clk):
    md = scripted_market()
    md.universe_rows = [{"symbol": s, "price": None, "volume": None} for s in ("ABCD", "EFGH", "MNOP")]
    md.movers = []
    md.price_volume = lambda syms: {"ABCD": {"price": 2.36, "volume": 1_500_000}}
    tips = Scout(cfg, db, md, clk).discover()
    abcd = next(t for t in tips if t["symbol"] == "ABCD" and t["source"] == "stocktwits:trending")
    assert abcd["eligible"] and abcd["price_at_tip"] == 2.36
    efgh = next(t for t in tips if t["symbol"] == "EFGH")
    assert not efgh["eligible"] and efgh["reject_reason"] == "no price"


def test_provider_selection(monkeypatch):
    cfg = Config()
    assert type(make_market_data(cfg, HttpClient(None, "t"))) is LiveMarketData  # auto, no keys
    cfg.data.provider = "alpaca"
    with pytest.raises(DataError, match="no Alpaca keys"):
        make_market_data(cfg, HttpClient(None, "t"))
    cfg.data.alpaca_key, cfg.data.alpaca_secret = "k", "s"
    assert isinstance(make_market_data(cfg, HttpClient(None, "t")), AlpacaMarketData)


# ------------------------------------------------------------------ key file
@pytest.mark.parametrize("text", [
    "ALPACA_KEY=PKABCDEF123456\nALPACA_SECRET=topsecretvalue123",
    "API Key: PKABCDEF123456\nSecret Key: topsecretvalue123",
    "PKABCDEF123456\ntopsecretvalue123",
    '$env:APCA_API_KEY_ID="PKABCDEF123456"\n$env:APCA_API_SECRET_KEY="topsecretvalue123"',
    "Endpoint https://paper-api.alpaca.markets/v2\nKey PKABCDEF123456\nSecret topsecretvalue123",
    "Endpoint: https://paper-api.alpaca.markets/v2\nAPI Key ID PKABCDEF123456\nSecret Key topsecretvalue123",
])
def test_alpaca_key_file_formats(text):
    assert parse_alpaca_file(text) == ("PKABCDEF123456", "topsecretvalue123")


def test_alpaca_keys_from_file_switch_provider_on(tmp_path, monkeypatch):
    for n in ("ALPACA_KEY", "ALPACA_SECRET", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
              "ALPACA_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".alpaca").write_text("ALPACA_KEY=PKABCDEF123456\nALPACA_SECRET=topsecretvalue123\n", encoding="utf-16")
    key, secret, where = find_alpaca_keys()
    assert (key, secret) == ("PKABCDEF123456", "topsecretvalue123") and ".alpaca" in where
    cfg = load_config()
    assert isinstance(make_market_data(cfg, HttpClient(None, "t")), AlpacaMarketData)


def test_previous_day_volume_lookup():
    http = Recorder({"/v2/stocks/bars": load("alpaca_bars_page2.json")})
    out = ap.Alpaca(http, "k", "s").previous_day(["ABCD"], date(2026, 11, 9))
    assert out["ABCD"] == {"close": 2.31, "volume": 5_400_000}


def test_backtest_universe_picker_prices_the_alpaca_stock_list():
    from stockagent.backtest import pick_universe

    md, _ = alpaca_md({"/v2/assets": load("alpaca_assets.json")})
    md.price_volume = lambda syms: {"ABCD": {"price": 2.36, "volume": 5_400_000}, "EFGH": {"price": 0.87, "volume": 800_000}}
    assert pick_universe(md, Config(), 10) == ["ABCD", "EFGH"]
