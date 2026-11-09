"""Mean reversion: smaller, more frequent wins. Useful as a counterweight
when momentum setups dry up, and it gives the learner a contrast."""
from __future__ import annotations

from typing import Any

import pandas as pd

from .. import indicators as ind
from .base import Signal, SignalContext, Strategy, clamp, prior_atr


class OversoldBounce(Strategy):
    name = "oversold_bounce"
    phases = ("close",)
    description = "RSI(2) under 10 in a stock still above its 100-day average; exit on a close above the 5-day average."

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        if len(df) < 105:
            return None
        last = float(df["close"].iloc[-1])
        if last < 1.0:
            return None  # sub-$1 names drift toward reverse splits; no bounce bias there
        r2 = float(ind.rsi(df["close"], 2).iloc[-1])
        if r2 > 10:
            return None
        sma100 = float(ind.sma(df["close"], 100).iloc[-1])
        if last < sma100:
            return None
        if ctx.catalyst and (ctx.catalyst.score < -1 or ctx.catalyst.dilution_risk):
            return None  # it is falling for a reason
        a = prior_atr(df)
        if not a:
            return None
        stretch = (float(ind.sma(df["close"], 5).iloc[-1]) - last) / last
        score = 0.5 * (1 - r2 / 10) + 0.3 * min(stretch / 0.1, 1) + 0.2 * min((last / sma100 - 1) / 0.3, 1)
        stop = clamp(2.0 * a, 0.05 * last, 0.15 * last)
        return Signal(
            symbol, self.name, round(max(score, 0.05), 4), last, stop, 1.5, 3,
            f"RSI(2) {r2:.1f} above the 100-day average {sma100:.2f}",
            atr=a, data={"rsi2": r2, "sma100": sma100, "stretch": stretch},
        )

    def exit_reason(self, trade: dict[str, Any], df: pd.DataFrame, ctx: SignalContext) -> str | None:
        if len(df) < 6:
            return None
        if float(df["close"].iloc[-1]) > float(ind.sma(df["close"], 5).iloc[-1]):
            return "reverted above 5-day average"
        return None
