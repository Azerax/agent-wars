import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from conftest import FIXTURES, FakeMarketData, at

from stockagent import clock as mcal
from stockagent.data import sec, social
from stockagent.learning import SourceLearner, StrategyLearner
from stockagent.scout import Scout


def scripted_market() -> FakeMarketData:
    md = FakeMarketData()
    md.universe_rows = [
        {"symbol": "ABCD", "name": "Abcd", "price": 2.31, "change_pct": 31.0, "volume": 5_400_000},
        {"symbol": "EFGH", "name": "Efgh", "price": 0.87, "change_pct": -3.0, "volume": 800_000},
        {"symbol": "BIGG", "name": "Big", "price": 145.0, "change_pct": 6.0, "volume": 12_000_000},
        {"symbol": "ABCDW", "name": "Abcd warrant", "price": 0.05, "change_pct": 25.0, "volume": 300_000},
        {"symbol": "LOWV", "name": "Thin", "price": 1.5, "change_pct": 9.0, "volume": 20_000},
    ]
    md.movers = [
        {"symbol": "ABCD", "price": 2.31, "change_pct": 31.2, "volume": 5_400_000, "avg_volume": 900_000, "screener": "day_gainers"},
        {"symbol": "BIGG", "price": 145.2, "change_pct": 6.1, "volume": 12_000_000, "avg_volume": 8_000_000, "screener": "day_gainers"},
    ]
    md.trending = [{"symbol": "ABCD", "rank": 1}, {"symbol": "ZZZZ", "rank": 2}]
    md.posts = {"pennystocks": social.parse_reddit_listing(json.loads((FIXTURES / "reddit_hot.json").read_text()))}
    filings = sec.parse_current_feed((FIXTURES / "sec_current_8k.xml").read_text())
    for f in filings:
        f["symbol"] = {1234567: "ABCD", 7654321: "EFGH"}[f["cik"]]
        f["dilution"] = False
    md.filing_list = filings
    md.headlines = {"ABCD": [{"title": "Abcd wins $12 million government contract",
                              "published": datetime(2026, 11, 2, 12, tzinfo=timezone.utc)}]}
    return md


def test_discover_records_every_tip_with_eligibility(cfg, db, clk):
    md = scripted_market()
    scout = Scout(cfg, db, md, clk)
    tips = scout.discover()
    by = {(t["symbol"], t["source"]): t for t in tips}
    assert by[("ABCD", "yahoo:day_gainers")]["eligible"]
    assert not by[("BIGG", "yahoo:day_gainers")]["eligible"]
    assert "above" in by[("BIGG", "yahoo:day_gainers")]["reject_reason"]
    assert not by[("ZZZZ", "stocktwits:trending")]["eligible"]
    assert ("ABCD", "sec:8-K") in by  # item 1.01 is a positive filing
    assert ("EFGH", "sec:8-K") not in by  # 3.01 delisting notice is not a tip
    assert ("ABCD", "reddit:pennystocks") in by
    assert all(sym != "ABCDW" for sym, _ in by)  # warrants never become tips
    assert db.scalar("SELECT COUNT(*) FROM tips") == len(tips)


def test_rank_builds_watchlist_with_catalyst(cfg, db, clk):
    scout = Scout(cfg, db, scripted_market(), clk)
    cands = scout.run()
    assert cands[0]["symbol"] == "ABCD"
    assert len(cands[0]["sources"]) >= 4
    assert cands[0]["catalyst"].score > 0
    row = db.one("SELECT * FROM watchlist WHERE symbol='ABCD'")
    assert row and row["catalyst_score"] > 0
    assert db.scalar("SELECT COUNT(*) FROM journal WHERE event='scout'") == 1


def test_same_day_repeat_tips_are_not_double_counted(cfg, db, clk):
    scout = Scout(cfg, db, scripted_market(), clk)
    first = scout.discover()
    scout.discover()
    assert db.scalar("SELECT COUNT(*) FROM tips") == len(first)


def test_a_failing_source_is_journaled_and_others_still_work(cfg, db, clk):
    md = scripted_market()
    md.fail = {"stocktwits", "filings"}
    scout = Scout(cfg, db, md, clk)
    cands = scout.run()
    assert cands and cands[0]["symbol"] == "ABCD"
    msgs = [r["message"] for r in db.all("SELECT message FROM journal WHERE event='source_failed'")]
    assert any("stocktwits" in m for m in msgs) and any("sec_filings" in m for m in msgs)


def test_evaluate_tips_measures_forward_returns(cfg, db):
    tip_day = date(2026, 11, 2)
    days = mcal.trading_days_in_range(date(2026, 10, 1), date(2026, 11, 9))
    closes = {d: 2.0 for d in days}
    closes[date(2026, 11, 3)] = 2.2
    closes[date(2026, 11, 6)] = 4.0
    df = pd.DataFrame({"open": list(closes.values()), "high": list(closes.values()), "low": list(closes.values()),
                       "close": list(closes.values()), "volume": [1e6] * len(closes)},
                      index=pd.DatetimeIndex([pd.Timestamp(d, tz=mcal.ET) for d in closes]))
    md = FakeMarketData({"ABCD": df})
    db.insert("tips", {"ts": at(tip_day, 8).isoformat(), "day": tip_day.isoformat(), "symbol": "ABCD",
                       "source": "nasdaq:gainers", "kind": "mover", "raw_score": 0.5, "price_at_tip": 2.0, "eligible": 1})
    clk = mcal.SimClock(at(date(2026, 11, 9), 8))
    assert Scout(cfg, db, md, clk).evaluate_tips() == 1
    t = db.one("SELECT * FROM tips")
    assert t["fwd_1d"] == pytest.approx(0.1) and t["fwd_4d"] == pytest.approx(1.0)
    stats = SourceLearner(db).stats()["nasdaq:gainers"]
    assert stats["doubled_in_4d"] == 1


def _trade(db, strategy, net, r):
    db.insert("trades", {"account_id": 1, "symbol": "X", "strategy": strategy, "status": "closed",
                         "opened_at": "2026-11-02T10:00:00-05:00", "net_pnl": net, "r_multiple": r, "fees": 0.02})


def test_strategy_learner_quarantines_losers_and_favours_winners(cfg, db):
    for _ in range(15):
        _trade(db, "loser", -1.0, -1.0)
    for _ in range(10):
        _trade(db, "winner", 2.0, 2.0)
    learner = StrategyLearner(db, 1, cfg.agent, seed=1)
    stats = learner.stats()
    assert stats["loser"]["quarantined"] and not stats["winner"]["quarantined"]
    w = learner.weights(["loser", "winner", "untested"], sample=False)
    assert w["loser"] == 0.0
    assert w["winner"] > w["untested"] > 0


def test_source_learner_shrinks_toward_neutral(db):
    for i in range(30):
        db.insert("tips", {"ts": "t", "day": "2026-11-02", "symbol": f"S{i}", "source": "good", "kind": "k",
                           "raw_score": 1, "eligible": 1, "fwd_1d": 0.05, "evaluated_1d": 1})
        db.insert("tips", {"ts": "t", "day": "2026-11-02", "symbol": f"S{i}", "source": "bad", "kind": "k",
                           "raw_score": 1, "eligible": 1, "fwd_1d": -0.05, "evaluated_1d": 1})
    db.insert("tips", {"ts": "t", "day": "2026-11-02", "symbol": "S", "source": "new", "kind": "k",
                       "raw_score": 1, "eligible": 1, "fwd_1d": 0.5, "evaluated_1d": 1})
    w = SourceLearner(db).weights()
    assert w["good"] > 1.0 > w["bad"]
    assert w["new"] < 1.5  # one lucky tip is not a track record
