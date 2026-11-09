"""Momentum strategies: the setups behind most multi-day doubles in small caps."""
from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from .base import (
    Signal,
    SignalContext,
    Strategy,
    clamp,
    close_loc,
    day_change,
    prior_atr,
    projected_rel_volume,
)


class MomentumBreakout(Strategy):
    name = "momentum_breakout"
    phases = ("close", "intraday")
    description = "New 20-day high on 2x+ relative volume, closing near the high of the day."

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        if len(df) < 30:
            return None
        last = float(df["close"].iloc[-1])
        hh20 = float(df["high"].iloc[-21:-1].max())
        if last <= hh20:
            return None
        chg = day_change(df)
        if chg < 0.04:
            return None
        rv = projected_rel_volume(df, ctx)
        if rv < 2.0:
            return None
        cl = close_loc(df)
        if cl < 0.6:
            return None
        a = prior_atr(df)
        if not a:
            return None
        sma20 = float(ind.sma(df["close"], 20).iloc[-1])
        sma50 = ind.sma(df["close"], 50).iloc[-1]
        trend = 1.0 if (pd.notna(sma50) and sma20 > float(sma50)) else 0.5 if last > sma20 else 0.0
        score = 0.35 * min(rv / 6, 1) + 0.25 * min(chg / 0.25, 1) + 0.2 * cl + 0.2 * trend
        if chg > 0.5:
            score *= 0.6  # already up 50%+ today: late, and the spread on a runner is wide
        stop = clamp(1.5 * a, 0.04 * last, 0.15 * last)
        return Signal(
            symbol, self.name, round(score, 4), last, stop, 2.5, 4,
            f"breakout over 20d high {hh20:.2f}, +{chg:.0%} on {rv:.1f}x volume, closing in top {1 - cl:.0%} of range",
            atr=a, data={"hh20": hh20, "rel_volume": rv, "change": chg, "close_location": cl},
        )


class SqueezeBreakout(Strategy):
    name = "squeeze_breakout"
    phases = ("close", "intraday")
    description = "Bollinger bandwidth near a 6-month low, then a close above the upper band on volume."

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        if len(df) < 80:
            return None
        bw = ind.bandwidth(df["close"])
        pr = ind.percentile_rank(bw, 120)
        prior_pr = pr.iloc[-2]
        if pd.isna(prior_pr) or prior_pr > 0.2:
            return None
        _lo, _mid, hi = ind.bollinger(df["close"])
        last = float(df["close"].iloc[-1])
        if last <= float(hi.iloc[-1]):
            return None
        chg = day_change(df)
        rv = projected_rel_volume(df, ctx)
        if chg < 0.03 or rv < 1.5:
            return None
        a = prior_atr(df)
        if not a:
            return None
        score = 0.4 * (1 - float(prior_pr)) + 0.3 * min(rv / 4, 1) + 0.3 * min(chg / 0.15, 1)
        stop = clamp(1.2 * a, 0.03 * last, 0.12 * last)
        return Signal(
            symbol, self.name, round(score, 4), last, stop, 3.0, 4,
            f"volatility squeeze (bandwidth pct {float(prior_pr):.0%}) released +{chg:.0%} on {rv:.1f}x volume",
            atr=a, data={"bandwidth_pct": float(prior_pr), "rel_volume": rv, "change": chg},
        )


class GapAndGo(Strategy):
    name = "gap_and_go"
    phases = ("open", "intraday")
    description = "Gap up 8%+ that holds above the open early in the session."

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        if len(df) < 25:
            return None
        prev_close = float(df["close"].iloc[-2])
        today_open = float(df["open"].iloc[-1])
        gap = today_open / prev_close - 1
        if gap < 0.08 or gap > 0.6:
            return None
        sma20_prev = ind.sma(df["close"].iloc[:-1], 20).iloc[-1]
        if pd.isna(sma20_prev) or prev_close < 0.9 * float(sma20_prev):
            return None
        a = prior_atr(df)
        if not a:
            return None
        last = float(df["close"].iloc[-1])
        rv = None
        cat = ctx.catalyst.score if ctx.catalyst else None
        if ctx.phase == "intraday":
            if ctx.minutes_since_open is None or ctx.minutes_since_open > 90:
                return None
            if last < today_open:
                return None  # gap is fading
            rv = projected_rel_volume(df, ctx)
            if rv < 3.0:
                return None
            if cat is not None and cat < 0:
                return None
        prev_rv = float(df["volume"].iloc[-2] / df["volume"].iloc[-22:-2].mean()) if df["volume"].iloc[-22:-2].mean() > 0 else 1.0
        score = 0.4 * min(gap / 0.3, 1) + 0.3 * min((rv or prev_rv) / 6, 1) + 0.3 * (1.0 if (cat or 0) > 1 else 0.5 if cat is None else 0.2)
        stop = clamp(0.8 * a, 0.04 * last, 0.12 * last)
        return Signal(
            symbol, self.name, round(score, 4), last, stop, 2.0, 2,
            f"gap +{gap:.0%} over {prev_close:.2f}" + (f", holding above open on {rv:.1f}x projected volume" if rv else ""),
            atr=a, data={"gap": gap, "rel_volume": rv, "catalyst": cat},
        )
