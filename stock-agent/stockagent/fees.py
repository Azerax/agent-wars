"""Explicit transaction fees: commission plus the regulatory fees passed
through to the customer on US equity sales."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from .config import FeeConfig

CENT = Decimal("0.01")


def _ceil_cents(x: float) -> float:
    if x <= 0:
        return 0.0
    return float(Decimal(str(x)).quantize(CENT, rounding=ROUND_CEILING))


def _round_cents(x: float) -> float:
    return float(Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class FeeBreakdown:
    commission: float = 0.0
    sec_fee: float = 0.0
    taf_fee: float = 0.0
    cat_fee: float = 0.0

    @property
    def total(self) -> float:
        return round(self.commission + self.sec_fee + self.taf_fee + self.cat_fee, 2)


def compute_fees(cfg: FeeConfig, side: str, qty: int, price: float) -> FeeBreakdown:
    if qty <= 0:
        return FeeBreakdown()
    value = qty * price
    commission = cfg.commission_per_trade + cfg.commission_per_share * qty
    if commission > 0:
        commission = max(commission, cfg.commission_min)
        if cfg.commission_max_pct > 0:
            commission = min(commission, value * cfg.commission_max_pct)
    commission = _round_cents(commission)

    sec_fee = taf_fee = 0.0
    if side == "sell":
        # Brokers round each regulatory fee up to the next cent, so even a $5
        # sale pays at least $0.01 of each.
        sec_fee = _ceil_cents(value * cfg.sec_fee_per_million / 1_000_000)
        if cfg.finra_taf_per_share > 0:
            taf_fee = min(_ceil_cents(qty * cfg.finra_taf_per_share), cfg.finra_taf_max)
    cat_fee = _ceil_cents(qty * cfg.cat_fee_per_share) if cfg.cat_fee_per_share > 0 else 0.0
    return FeeBreakdown(commission, sec_fee, taf_fee, cat_fee)


def round_trip_fee_estimate(cfg: FeeConfig, qty: int, entry: float, exit_: float) -> float:
    return compute_fees(cfg, "buy", qty, entry).total + compute_fees(cfg, "sell", qty, exit_).total
