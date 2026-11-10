"""Fill pricing for the simulated exchange.

A real broker fills a market buy at the ask and a market sell at the bid.
When the data feed gives a quote we use it; when it only gives a last price
we model the spread from price and liquidity, which is where most of the cost
of trading low-priced stocks hides.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .config import ExecutionConfig


def tick_size(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def round_to_tick(price: float, direction: str = "nearest") -> float:
    tick = tick_size(price)
    n = price / tick
    if direction == "up":
        n = math.ceil(n - 1e-9)
    elif direction == "down":
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    return round(n * tick, 4)


def estimate_spread(price: float, avg_dollar_volume: float | None, cfg: ExecutionConfig) -> float:
    """Full bid-ask spread in dollars."""
    if price <= 0:
        return 0.0
    dv_m = max((avg_dollar_volume or 0.0) / 1_000_000, 0.05)
    pct = cfg.base_spread_pct + cfg.liquidity_spread_coef / math.sqrt(dv_m)
    return max(tick_size(price), price * pct)


@dataclass
class Quote:
    symbol: str
    last: float
    ts: datetime | None = None
    bid: float | None = None
    ask: float | None = None
    # Extremes traded since the previous quote we processed. Lets the broker
    # see a stop that was hit between two polls.
    high: float | None = None
    low: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    avg_dollar_volume: float | None = None
    extra: dict = field(default_factory=dict)

    def mid(self) -> float:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2
        return self.last

    def bid_ask(self, cfg: ExecutionConfig) -> tuple[float, float]:
        """Return (bid, ask), modelling them from the last price if missing,
        crossed, stale, or far wider than this stock's normal spread."""
        model = estimate_spread(self.last, self.avg_dollar_volume, cfg)
        if self.bid and self.ask and self.ask >= self.bid > 0:
            # A stale quote far from the last trade is worse than a model, and
            # so is one venue's quote (Alpaca's free feed is IEX only), which
            # can be many times wider than the whole market's best bid/offer.
            stale = abs(self.mid() - self.last) / self.last >= 0.05
            too_wide = (self.ask - self.bid) > max(3 * model, 2 * tick_size(self.last))
            if not stale and not too_wide:
                return self.bid, self.ask
        half = model / 2
        return round_to_tick(max(self.last - half, tick_size(self.last)), "down"), round_to_tick(
            self.last + half, "up"
        )


def buy_price(ask: float, cfg: ExecutionConfig, slippage: bool = True) -> float:
    p = ask * (1 + cfg.market_slippage_pct) if slippage else ask
    return round_to_tick(p, "up")


def sell_price(bid: float, cfg: ExecutionConfig, slippage: bool = True) -> float:
    p = bid * (1 - cfg.market_slippage_pct) if slippage else bid
    return max(round_to_tick(p, "down"), tick_size(p))
