import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from conftest import FakeMarketData, at
from synthetic import make_bars

from stockagent import clock as mcal
from stockagent.agent import TradingAgent
from stockagent.backtest import Backtester
from stockagent.broker import SimBroker
from stockagent.config import Config, LLMConfig
from stockagent.execution import Quote
from stockagent.llm import CatalystLLM
from stockagent.report import build_state, text_report
from stockagent.server import make_server

MON = date(2026, 11, 2)


def history(end: date, n: int = 90, price: float = 2.0) -> pd.DataFrame:
    days = [d for d in mcal.trading_days_in_range(end - timedelta(days=200), end)][-n:]
    closes = np.array([price * (1 + 0.01 * np.sin(i / 3)) for i in range(n)])
    return pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes, "volume": [1_000_000.0] * n},
        index=pd.DatetimeIndex([pd.Timestamp(d, tz=mcal.ET) for d in days]),
    )


@pytest.fixture
def market():
    md = FakeMarketData({"ABCD": history(date(2026, 10, 30))})
    md.universe_rows = [{"symbol": "ABCD", "name": "Abcd", "price": 2.0, "change_pct": 0.0, "volume": 3_000_000}]
    md.movers = [{"symbol": "ABCD", "price": 2.0, "change_pct": 12.0, "volume": 3_000_000, "avg_volume": 1_000_000,
                  "screener": "small_cap_gainers"}]
    md.trending = [{"symbol": "ABCD", "rank": 1}]
    md.headlines = {"ABCD": [{"title": "Abcd wins $12 million government contract",
                              "published": datetime(2026, 11, 2, 12, tzinfo=timezone.utc)}]}
    return md


def quote(last, **kw):
    base = dict(open=2.05, day_high=max(2.36, last), day_low=2.03, prev_close=2.0, volume=3_000_000,
                avg_dollar_volume=2_000_000)
    base.update(kw)
    return Quote("ABCD", last, ts=None, **base)


def test_full_trading_day(cfg, db, market):
    cfg.agent.strategies = ["momentum_breakout", "catalyst_momentum", "social_momentum"]
    clk = mcal.SimClock(at(MON, 8, 0))
    broker = SimBroker(db, cfg, clk)
    broker.open_account(100.0)
    agent = TradingAgent(cfg, db, market, broker, clk)

    assert agent.tick() == "premarket"
    assert db.get_meta("premarket_day") == MON.isoformat()
    assert db.one("SELECT * FROM watchlist WHERE symbol='ABCD'")

    clk.set(at(MON, 10, 0))
    market.live["ABCD"] = quote(2.35)
    assert agent.tick() == "open"
    pos = broker.position("ABCD")
    assert pos is not None, db.all("SELECT decision, decision_reason FROM signals")
    entered = db.all("SELECT * FROM signals WHERE decision='entered'")
    assert len(entered) == 1
    trade = broker.open_trades()[0]
    assert trade["initial_stop"] < pos["avg_price"] < trade["target"]
    risk = trade["initial_risk"]
    assert risk <= 100 * cfg.risk.risk_per_trade_pct + 0.5

    clk.set(at(MON, 11, 0))
    market.live["ABCD"] = quote(3.0, day_high=3.0)
    agent.tick()
    t = broker.trade(trade["id"])
    assert t["status"] == "closed" and t["exit_reason"] == "profit_target"
    assert t["exit_value"] / t["exit_qty"] == pytest.approx(trade["target"], abs=0.011)  # resting limit, not the 2.98 bid
    assert t["net_pnl"] > 0

    clk.set(at(MON, 16, 5))
    agent.tick()
    assert db.get_meta("eod_day") == MON.isoformat()
    close = db.one("SELECT * FROM equity WHERE kind='close'")
    assert close["equity"] == pytest.approx(100 + t["net_pnl"])
    assert db.one("SELECT * FROM cycles WHERE number=1")["start_equity"] == 100.0
    events = {r["event"] for r in db.all("SELECT event FROM journal")}
    assert {"premarket", "scout", "entry", "fill", "trade_closed", "end_of_day"} <= events

    state = build_state(cfg, db, broker, clk)
    assert state["metrics"]["trades"] == 1
    report = text_report(state)
    assert "doubling path" in report and "Tip sources" in report


def test_time_stop_and_gate_after_daily_loss(cfg, db, market):
    cfg.agent.strategies = ["momentum_breakout"]
    clk = mcal.SimClock(at(MON, 8, 0))
    broker = SimBroker(db, cfg, clk)
    broker.open_account(100.0)
    agent = TradingAgent(cfg, db, market, broker, clk)
    agent.tick()
    clk.set(at(MON, 10, 0))
    market.live["ABCD"] = quote(2.35)
    agent.tick()
    trade = broker.open_trades()[0]
    # Four trading days later, near the close, the position is closed on time.
    day5 = mcal.next_trading_day(MON, 4)
    for d in mcal.trading_days_in_range(MON + timedelta(days=1), day5):
        clk.set(at(d, 8, 0))
        agent.tick()
    clk.set(at(day5, 15, 55))
    market.live["ABCD"] = quote(2.40, day_high=2.41, day_low=2.38, open=2.39)
    agent.tick()
    t = broker.trade(trade["id"])
    assert t["status"] == "closed" and t["exit_reason"].startswith("time_stop")


def test_backtest_invariants():
    cfg = Config()
    data = {f"SYN{i}": make_bars(date(2025, 3, 3), date(2026, 9, 30), seed=10 + i, price=1.2 + 0.4 * i, jumps=10)
            for i in range(6)}
    res = Backtester(cfg, data, date(2026, 3, 2), date(2026, 9, 30)).run()
    m = res["metrics"]
    assert m["trading_days"] > 100
    assert m["trades"] > 0
    for t in res["trades"]:
        if t["status"] == "closed":
            assert t["net_pnl"] == pytest.approx(t["exit_value"] - t["entry_value"] - t["fees"], abs=1e-6)
            assert t["entry_qty"] == t["exit_qty"]
    assert m["fees_paid"] > 0
    assert m["final_equity"] == pytest.approx(res["equity"][-1][1])
    assert all(e > 0 for _, e in res["equity"])


def test_backtest_skips_live_only_strategies():
    cfg = Config()
    data = {"SYN": make_bars(date(2025, 3, 3), date(2026, 6, 30), seed=3)}
    bt = Backtester(cfg, data, date(2026, 3, 2), date(2026, 6, 30))
    assert not any(s.live_only for s in bt.engine.strategies)


def test_api_state_orders_and_cancel(cfg, db, clk, broker):
    httpd = make_server(cfg, db, broker, clk, "127.0.0.1", 0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        state = json.load(urllib.request.urlopen(base + "/api/state"))
        assert state["account"]["equity"] == 100.0 and state["pace"]["cycle_target"] == 200.0
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "Stock Agent Desk" in html
        req = urllib.request.Request(base + "/api/orders", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"symbol": "abcd", "side": "buy", "qty": 10, "type": "limit",
                                                      "limit_price": 2.0}).encode())
        order = json.load(urllib.request.urlopen(req))
        assert order["status"] == "open" and order["symbol"] == "ABCD"
        bad = urllib.request.Request(base + "/api/orders", method="POST", data=json.dumps(
            {"symbol": "ABCD", "side": "buy", "qty": 1000, "type": "limit", "limit_price": 2.0}).encode())
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad)
        assert err.value.code == 422
        cancelled = json.load(urllib.request.urlopen(
            urllib.request.Request(base + f"/api/orders/{order['id']}", method="DELETE")))
        assert cancelled["status"] == "cancelled"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/nope")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_llm_disabled_without_credentials(monkeypatch, tmp_path):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    llm = CatalystLLM(LLMConfig(enabled="auto"))
    assert not llm.enabled and "credentials" in llm.disabled_reason


class _FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.response


def _fake_client(stop_reason="end_turn", payload=None):
    text = json.dumps(payload or {"score": 3.5, "catalyst_type": "contract", "dilution_risk": False,
                                  "confidence": 0.8, "rationale": "Large contract relative to market cap."})
    resp = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                           content=[SimpleNamespace(type="text", text=text)])
    msgs = _FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_llm_grade_parses_structured_output_and_respects_budget():
    llm = CatalystLLM(LLMConfig(enabled="on", max_calls_per_day=1))
    client, msgs = _fake_client()
    llm._client = client
    heads = [{"title": "Abcd wins $12 million contract", "published": datetime(2026, 11, 2, tzinfo=timezone.utc)}]
    a = llm.grade("ABCD", heads, [], MON)
    assert a.score == 3.5 and a.grader.startswith("claude") and not a.dilution_risk
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "<evidence>" in call["messages"][0]["content"]
    assert llm.grade("ABCD", heads, [], MON) is None  # daily budget of 1 spent


def test_llm_refusal_falls_back():
    llm = CatalystLLM(LLMConfig(enabled="on"))
    llm._client, _ = _fake_client(stop_reason="refusal")
    assert llm.grade("ABCD", [], [], MON) is None


def test_stop_hit_between_polls_seen_from_new_session_low(cfg, db, market):
    """Without minute bars, a new day low since the last poll still triggers the stop."""
    cfg.agent.strategies = ["momentum_breakout"]
    clk = mcal.SimClock(at(MON, 8, 0))
    broker = SimBroker(db, cfg, clk)
    broker.open_account(100.0)
    agent = TradingAgent(cfg, db, market, broker, clk)
    agent.tick()
    clk.set(at(MON, 10, 0))
    market.live["ABCD"] = quote(2.35, day_low=2.30)
    agent.tick()
    trade = broker.open_trades()[0]
    assert trade["initial_stop"] < 2.30
    clk.set(at(MON, 10, 1))
    agent.tick()  # records the session range with the position held
    clk.set(at(MON, 10, 2))
    # Price dipped through the stop and recovered before this poll.
    market.live["ABCD"] = quote(2.34, day_low=trade["initial_stop"] - 0.05)
    agent.tick()
    t = broker.trade(trade["id"])
    assert t["status"] == "closed" and t["exit_reason"] == "stop_loss"
