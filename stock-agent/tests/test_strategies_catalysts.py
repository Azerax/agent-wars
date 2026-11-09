from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from stockagent import catalysts
from stockagent import clock as mcal
from stockagent.catalysts import CatalystAssessment
from stockagent.strategies import SignalContext
from stockagent.strategies.catalyst import CatalystMomentum, SocialMomentum
from stockagent.strategies.momentum import GapAndGo, MomentumBreakout, SqueezeBreakout
from stockagent.strategies.reversion import OversoldBounce


def frame(closes, volumes=None, spread=0.02):
    days = mcal.trading_days_in_range(date(2025, 1, 2), date(2026, 12, 31))[: len(closes)]
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    vol = np.asarray(volumes if volumes is not None else [1_000_000] * len(closes), dtype=float)
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, closes) * (1 + spread / 2), "low": np.minimum(opens, closes) * (1 - spread / 2),
         "close": closes, "volume": vol},
        index=pd.DatetimeIndex([pd.Timestamp(d, tz=mcal.ET) for d in days]),
    )


def flat(n=80, price=2.0):
    return [price * (1 + 0.01 * np.sin(i / 3)) for i in range(n)]


BT = SignalContext(mode="backtest", phase="close")


def test_breakout_fires_on_new_high_with_volume():
    df = frame(flat() + [2.35], [1_000_000] * 80 + [4_000_000])
    df.iloc[-1, df.columns.get_loc("high")] = 2.36
    sig = MomentumBreakout().evaluate("ABCD", df, BT)
    assert sig is not None
    assert sig.ref_price == 2.35
    assert 0.04 * 2.35 <= sig.stop_distance <= 0.15 * 2.35
    assert sig.target_price > sig.ref_price > sig.stop_price
    assert "20d high" in sig.reason


def test_breakout_needs_volume():
    df = frame(flat() + [2.35], [1_000_000] * 81)
    assert MomentumBreakout().evaluate("ABCD", df, BT) is None


def test_breakout_projects_partial_session_volume():
    df = frame(flat() + [2.35], [1_000_000] * 80 + [900_000])
    df.iloc[-1, df.columns.get_loc("high")] = 2.36
    early = SignalContext(mode="live", phase="intraday", partial_bar=True, session_fraction=0.2)
    assert MomentumBreakout().evaluate("ABCD", df, early) is not None
    assert MomentumBreakout().evaluate("ABCD", df, BT) is None


def test_squeeze_breakout():
    calm = [2.0 + 0.002 * ((i % 4) - 1.5) for i in range(140)]
    wild = [2.0 * (1 + 0.08 * np.sin(i)) for i in range(60)]
    df = frame(wild + calm + [2.12], [1_000_000] * 200 + [2_500_000])
    sig = SqueezeBreakout().evaluate("ABCD", df, BT)
    assert sig is not None and sig.strategy == "squeeze_breakout"


def test_gap_and_go_open_phase_and_fading_gap():
    base = flat(60)
    df = frame(base + [2.3], [1_000_000] * 61)
    df.iloc[-1, df.columns.get_loc("open")] = 2.3
    sig = GapAndGo().evaluate("ABCD", df, SignalContext(mode="backtest", phase="open", partial_bar=True))
    assert sig is not None and sig.data["gap"] > 0.08
    df.iloc[-1, df.columns.get_loc("close")] = 2.2  # trading below its open: the gap is fading
    live = SignalContext(mode="live", phase="intraday", partial_bar=True, session_fraction=0.1, minutes_since_open=20)
    assert GapAndGo().evaluate("ABCD", df, live) is None


def test_oversold_bounce_and_its_exit():
    up = [1.5 + 0.01 * i for i in range(110)]
    df = frame(up + [up[-1] * 0.93, up[-1] * 0.86], [1_000_000] * 112)
    s = OversoldBounce()
    sig = s.evaluate("ABCD", df, BT)
    assert sig is not None and sig.data["rsi2"] < 10
    later = frame(up + [up[-1] * 0.93, up[-1] * 0.86, up[-1] * 1.01], [1_000_000] * 113)
    assert s.exit_reason({}, later, BT) is not None
    bad = SignalContext(mode="backtest", phase="close", catalyst=CatalystAssessment("ABCD", -3, ["offering"], True))
    assert s.evaluate("ABCD", df, bad) is None


def test_catalyst_and_social_strategies_need_confirmation():
    df = frame(flat(40) + [2.3], [1_000_000] * 40 + [4_000_000])
    df.iloc[-1, df.columns.get_loc("open")] = 2.05
    good = CatalystAssessment("ABCD", 3.0, ["fda_approval"], False)
    ctx = SignalContext(mode="live", phase="intraday", partial_bar=True, session_fraction=0.5, catalyst=good)
    assert CatalystMomentum().evaluate("ABCD", df, ctx) is not None
    ctx.catalyst = CatalystAssessment("ABCD", 3.0, ["fda_approval", "offering"], True)
    assert CatalystMomentum().evaluate("ABCD", df, ctx) is None
    sctx = SignalContext(mode="live", phase="intraday", partial_bar=True, session_fraction=0.5,
                         social={"stocktwits_rank": 2, "bull_ratio": 0.8})
    assert SocialMomentum().evaluate("ABCD", df, sctx) is not None
    sctx.social = {"stocktwits_rank": 2, "bull_ratio": 0.3}
    assert SocialMomentum().evaluate("ABCD", df, sctx) is None


@pytest.mark.parametrize("title,positive,tag", [
    ("Abcd Therapeutics Announces FDA Approval of Lead Candidate", True, "fda_approval"),
    ("Abcd wins $12 million government contract", True, "contract"),
    ("XYZ to be acquired by MegaCorp for $4.10 per share in cash", True, "acquisition_target"),
    ("Abcd Prices $5 Million Registered Direct Offering", False, "offering"),
    ("Abcd announces 1-for-20 reverse stock split", False, "reverse_split"),
    ("Abcd receives Nasdaq deficiency notice regarding minimum bid price", False, "delisting_risk"),
    ("Abcd files for Chapter 11 bankruptcy protection", False, "bankruptcy"),
    ("FDA issues complete response letter for Abcd's drug", False, "fda_setback"),
])
def test_headline_lexicon(title, positive, tag):
    score, tags = catalysts.score_headline(title)
    assert (score > 0) == positive
    assert tag in tags


def test_assess_combines_news_and_filings_and_flags_dilution():
    now = datetime(2026, 11, 5, 18, tzinfo=timezone.utc)
    heads = [{"title": "Abcd wins $12 million government contract", "published": now}]
    a = catalysts.assess("ABCD", heads, [{"form": "8-K", "items": ["1.01"], "dilution": False}], now)
    assert a.score > 2 and not a.dilution_risk
    b = catalysts.assess("ABCD", heads, [{"form": "424B5", "items": [], "dilution": True}], now)
    assert b.dilution_risk and b.score < a.score
    stale = [{"title": "Abcd wins $12 million government contract", "published": datetime(2026, 10, 1, tzinfo=timezone.utc)}]
    assert catalysts.assess("ABCD", stale, [], now).score == 0
