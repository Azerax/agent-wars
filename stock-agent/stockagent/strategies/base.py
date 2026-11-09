"""What a strategy sees and what it returns.

A strategy looks at one symbol's daily bars (the last row is today: complete
at the close in a backtest, partial during a live session) plus whatever the
scout learned about it, and either proposes a trade or returns None. It never
sizes, never places orders: risk.py and the agent do that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .. import indicators as ind
from ..catalysts import CatalystAssessment
from ..execution import Quote


@dataclass
class SignalContext:
    mode: str  # "live" | "backtest"
    phase: str  # "open" | "intraday" | "close"
    partial_bar: bool = False
    session_fraction: float = 1.0
    minutes_since_open: float | None = None
    quote: Quote | None = None
    catalyst: CatalystAssessment | None = None
    social: dict[str, Any] | None = None
    tip_score: float = 0.0


@dataclass
class Signal:
    symbol: str
    strategy: str
    score: float  # 0..1, the strategy's own conviction
    ref_price: float
    stop_distance: float  # dollars per share between entry and protective stop
    reward_multiple: float  # profit target = entry + reward_multiple * stop_distance
    max_hold_days: int
    reason: str
    atr: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def stop_price(self) -> float:
        return self.ref_price - self.stop_distance

    @property
    def target_price(self) -> float:
        return self.ref_price + self.reward_multiple * self.stop_distance


class Strategy:
    name = "base"
    phases: tuple[str, ...] = ("close", "intraday")
    live_only = False
    description = ""

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        raise NotImplementedError

    def exit_reason(self, trade: dict[str, Any], df: pd.DataFrame, ctx: SignalContext) -> str | None:
        """Optional strategy-specific exit, checked near the close."""
        return None


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def projected_rel_volume(df: pd.DataFrame, ctx: SignalContext, n: int = 20) -> float:
    """Relative volume, projecting a partial session's volume to a full day.
    Volume is front-loaded, so the projection is damped early in the session."""
    if len(df) < 6:
        return 0.0
    vol = float(df["volume"].iloc[-1])
    if ctx.partial_bar:
        frac = max(ctx.session_fraction, 0.15)
        vol = vol / (frac ** 0.75)
    avg = float(df["volume"].iloc[-n - 1 : -1].mean())
    return vol / avg if avg > 0 else 0.0


def prior_atr(df: pd.DataFrame, n: int = 14) -> float | None:
    """ATR as of yesterday, so a partial or explosive bar today does not inflate it."""
    a = ind.atr(df.iloc[:-1], n)
    if a.empty or pd.isna(a.iloc[-1]):
        return None
    return float(a.iloc[-1])


def day_change(df: pd.DataFrame) -> float:
    return float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) if len(df) >= 2 else 0.0


def close_loc(df: pd.DataFrame) -> float:
    row = df.iloc[-1]
    rng = row["high"] - row["low"]
    return float((row["close"] - row["low"]) / rng) if rng > 0 else 0.5
