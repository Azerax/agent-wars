"""Strategies driven by what the scout found: news and crowd attention.
Live only, because there is no honest way to replay historical headlines
and chatter as they looked at the time."""
from __future__ import annotations

import pandas as pd

from .base import (
    Signal,
    SignalContext,
    Strategy,
    clamp,
    day_change,
    prior_atr,
    projected_rel_volume,
)


class CatalystMomentum(Strategy):
    name = "catalyst_momentum"
    phases = ("intraday",)
    live_only = True
    description = "A real positive catalyst (news or filing) confirmed by price up 5%+ on 2x+ volume, holding above the open."

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        cat = ctx.catalyst
        if not cat or cat.score < 2.0 or cat.dilution_risk or len(df) < 20:
            return None
        chg = day_change(df)
        rv = projected_rel_volume(df, ctx)
        last = float(df["close"].iloc[-1])
        if chg < 0.05 or rv < 2.0 or last < float(df["open"].iloc[-1]):
            return None
        a = prior_atr(df)
        if not a:
            return None
        score = 0.4 * min(cat.score / 4, 1) + 0.3 * min(rv / 6, 1) + 0.3 * min(chg / 0.3, 1)
        stop = clamp(1.2 * a, 0.05 * last, 0.15 * last)
        return Signal(
            symbol, self.name, round(score, 4), last, stop, 3.0, 4,
            f"catalyst {cat.score:+.1f} ({', '.join(cat.tags[:3])}) with +{chg:.0%} on {rv:.1f}x volume",
            atr=a, data={"catalyst": cat.to_dict(), "rel_volume": rv, "change": chg},
        )


class SocialMomentum(Strategy):
    name = "social_momentum"
    phases = ("intraday",)
    live_only = True
    description = "Trending on StockTwits or Reddit with bullish sentiment, confirmed by price and volume, no bad news."

    def evaluate(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> Signal | None:
        s = ctx.social or {}
        strength = 0.0
        if s.get("stocktwits_rank"):
            strength += max(0.0, 1 - (s["stocktwits_rank"] - 1) / 30)
        if s.get("reddit_weight"):
            strength += min(s["reddit_weight"] / 20, 1.0)
        strength = min(strength, 1.0)
        bull = s.get("bull_ratio")
        if strength < 0.3 or (bull is not None and bull < 0.6) or len(df) < 20:
            return None
        if ctx.catalyst and (ctx.catalyst.score < -0.5 or ctx.catalyst.dilution_risk):
            return None
        chg = day_change(df)
        rv = projected_rel_volume(df, ctx)
        if chg < 0.03 or rv < 1.5:
            return None
        a = prior_atr(df)
        if not a:
            return None
        last = float(df["close"].iloc[-1])
        score = 0.4 * strength + 0.3 * min(rv / 5, 1) + 0.3 * min(chg / 0.2, 1)
        stop = clamp(1.2 * a, 0.05 * last, 0.12 * last)
        return Signal(
            symbol, self.name, round(score, 4), last, stop, 2.0, 3,
            f"crowd attention {strength:.2f}" + (f", {bull:.0%} bullish" if bull is not None else "")
            + f", +{chg:.0%} on {rv:.1f}x volume",
            atr=a, data={"social": s, "rel_volume": rv, "change": chg},
        )
