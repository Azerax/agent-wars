"""Position sizing, circuit breakers, and the doubling-target scoreboard.

One rule is deliberate and worth stating: falling behind the target never
increases risk. Sizing up to "catch up" is how small accounts go to zero, and
a paper record that got lucky that way would teach the wrong lesson.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date

from . import clock as mcal
from .broker import SimBroker
from .config import Config
from .db import Database
from .execution import Quote, estimate_spread
from .fees import compute_fees
from .strategies.base import Signal


@dataclass
class PaceStatus:
    start_day: str
    starting_equity: float
    equity: float
    trading_day_index: int  # 0 on the first day
    target_today: float  # where the doubling path says equity should be at today's close
    vs_target: float  # equity / target_today
    cycle_number: int
    cycle_day: int  # 1..period
    cycle_start_equity: float
    cycle_target: float
    cycle_multiple: float
    days_left_in_cycle: int
    required_daily_return: float  # to still hit this cycle's target
    total_multiple: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SizeDecision:
    qty: int
    ok: bool
    reason: str
    est_cost: float = 0.0
    est_reward: float = 0.0
    est_risk: float = 0.0
    limit_price: float | None = None


class RiskManager:
    def __init__(self, cfg: Config, broker: SimBroker, db: Database, clock: mcal.Clock):
        self.cfg = cfg
        self.broker = broker
        self.db = db
        self.clock = clock

    # ----------------------------------------------------------------- target
    def _start_day(self) -> date:
        return date.fromisoformat(self.broker.account()["start_date"])

    def day_index(self, today: date | None = None) -> int:
        today = today or self.clock.now().date()
        return max(0, mcal.trading_days_between(self._start_day(), mcal.last_trading_day_on_or_before(today)))

    def target_equity(self, today: date | None = None) -> float:
        acct = self.broker.account()
        t = self.cfg.target
        return acct["starting_cash"] * t.multiple ** ((self.day_index(today) + 1) / t.period_trading_days)

    def ensure_cycle(self, today: date, equity: float) -> dict:
        period = self.cfg.target.period_trading_days
        idx = self.day_index(today)
        number = idx // period + 1
        row = self.db.one(
            "SELECT * FROM cycles WHERE account_id=? AND number=?", [self.broker.account_id, number]
        )
        if row:
            return row
        start_day = mcal.next_trading_day(self._start_day(), (number - 1) * period) if number > 1 else self._start_day()
        end_day = mcal.next_trading_day(start_day, period - 1)
        self.db.insert(
            "cycles",
            {
                "account_id": self.broker.account_id, "number": number, "start_day": start_day.isoformat(),
                "end_day": end_day.isoformat(), "start_equity": equity,
                "target_equity": equity * self.cfg.target.multiple,
            },
        )
        return self.db.one("SELECT * FROM cycles WHERE account_id=? AND number=?", [self.broker.account_id, number]) or {}

    def close_day(self, today: date, equity: float) -> dict:
        cyc = self.ensure_cycle(today, equity)
        if today.isoformat() >= cyc["end_day"]:
            multiple = equity / cyc["start_equity"] if cyc["start_equity"] else 0.0
            self.db.update(
                "cycles", cyc["id"],
                {"end_equity": equity, "multiple": round(multiple, 4), "hit": int(equity >= cyc["target_equity"])},
            )
        return cyc

    def pace(self, equity: float, today: date | None = None) -> PaceStatus:
        today = today or self.clock.now().date()
        acct = self.broker.account()
        period = self.cfg.target.period_trading_days
        idx = self.day_index(today)
        cyc = self.ensure_cycle(today, equity)
        cycle_day = idx % period + 1
        days_left = period - cycle_day + 1
        target_today = self.target_equity(today)
        required = (cyc["target_equity"] / equity) ** (1 / days_left) - 1 if equity > 0 else math.inf
        return PaceStatus(
            start_day=acct["start_date"],
            starting_equity=acct["starting_cash"],
            equity=round(equity, 4),
            trading_day_index=idx,
            target_today=round(target_today, 4),
            vs_target=round(equity / target_today, 4) if target_today else 0.0,
            cycle_number=cyc["number"],
            cycle_day=cycle_day,
            cycle_start_equity=round(cyc["start_equity"], 4),
            cycle_target=round(cyc["target_equity"], 4),
            cycle_multiple=round(equity / cyc["start_equity"], 4) if cyc["start_equity"] else 0.0,
            days_left_in_cycle=days_left,
            required_daily_return=round(required, 4),
            total_multiple=round(equity / acct["starting_cash"], 4),
        )

    # ----------------------------------------------------------------- gating
    def day_start_equity(self, today: date) -> float | None:
        row = self.db.one(
            "SELECT equity FROM equity WHERE account_id=? AND day=? AND kind='start' ORDER BY id LIMIT 1",
            [self.broker.account_id, today.isoformat()],
        )
        if row:
            return row["equity"]
        row = self.db.one(
            "SELECT equity FROM equity WHERE account_id=? AND day<? AND kind='close' ORDER BY id DESC LIMIT 1",
            [self.broker.account_id, today.isoformat()],
        )
        return row["equity"] if row else None

    def slots_used(self) -> int:
        held = {p["symbol"] for p in self.broker.positions()}
        pending = {o["symbol"] for o in self.broker.open_orders() if o["side"] == "buy"}
        return len(held | pending)

    def entry_gate(self, equity: float, as_of: date | None = None) -> tuple[bool, str]:
        r = self.cfg.risk
        acct = self.broker.account()
        if equity < acct["peak_equity"] * (1 - r.max_drawdown_halt_pct):
            return False, f"drawdown halt: equity ${equity:.2f} is over {r.max_drawdown_halt_pct:.0%} below peak ${acct['peak_equity']:.2f}"
        today = self.clock.now().date()
        start = self.day_start_equity(today)
        if start and equity < start * (1 - r.daily_loss_limit_pct):
            return False, f"daily loss limit: down {1 - equity / start:.0%} today"
        if self.slots_used() >= r.max_positions:
            return False, f"all {r.max_positions} position slots in use"
        bp = self.broker.buying_power(as_of)
        if bp < r.min_position_value:
            return False, f"buying power ${bp:.2f} below minimum position ${r.min_position_value:.2f} (sale proceeds settle T+1)"
        return True, "ok"

    def size(self, signal: Signal, quote: Quote, equity: float, as_of: date | None = None) -> SizeDecision:
        r = self.cfg.risk
        ex = self.cfg.execution
        bid, ask = quote.bid_ask(ex)
        entry = ask
        limit = round(entry * (1 + ex.max_chase_pct), 4 if entry < 1 else 2)
        stop_dist = signal.stop_distance
        if stop_dist <= 0 or entry <= 0:
            return SizeDecision(0, False, "invalid stop distance")
        risk_budget = equity * r.risk_per_trade_pct
        qty_risk = math.floor(risk_budget / stop_dist)
        per_share_reserve = limit * 1.0  # a limit order reserves its limit price
        bp = self.broker.buying_power(as_of)
        cap_value = min(equity * r.max_position_pct, bp)
        fee_room = compute_fees(self.cfg.fees, "buy", max(qty_risk, 1), limit).total
        qty_cap = math.floor(max(cap_value - fee_room, 0) / per_share_reserve)
        qty = min(qty_risk, qty_cap)
        if qty <= 0 or qty * entry < r.min_position_value:
            return SizeDecision(qty, False, f"position too small (${qty * entry:.2f}); buying power ${bp:.2f}", limit_price=limit)
        target = entry + signal.reward_multiple * stop_dist
        spread = (ask - bid) if quote.bid and quote.ask else estimate_spread(quote.last, quote.avg_dollar_volume, ex)
        fees = compute_fees(self.cfg.fees, "buy", qty, entry).total + compute_fees(self.cfg.fees, "sell", qty, target).total
        # Pay roughly a full spread over the round trip, plus slippage on the exit.
        est_cost = fees + qty * spread + qty * target * ex.market_slippage_pct
        est_reward = qty * (target - entry)
        if est_reward <= 0 or est_cost > r.max_cost_to_reward * est_reward:
            return SizeDecision(
                qty, False,
                f"costs ${est_cost:.2f} are {est_cost / max(est_reward, 1e-9):.0%} of the ${est_reward:.2f} reward",
                est_cost, est_reward, qty * stop_dist, limit,
            )
        return SizeDecision(
            qty, True,
            f"{qty} sh: risk ${qty * stop_dist:.2f} ({qty * stop_dist / equity:.1%} of equity), est costs ${est_cost:.2f}",
            est_cost, est_reward, qty * stop_dist, limit,
        )
