"""Decisions shared by the live agent and the backtester: which signals to
act on, how many shares, and how to manage a position once it is open.
Keeping them in one place means a backtest tests the code that trades."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from . import clock as mcal
from .broker import SimBroker
from .catalysts import CatalystAssessment
from .config import Config
from .db import Database, dumps
from .execution import Quote, tick_size
from .learning import StrategyLearner
from .risk import RiskManager
from .strategies import Signal, SignalContext, Strategy, build

log = logging.getLogger(__name__)


@dataclass
class EntryCandidate:
    signal: Signal
    quote: Quote
    tip_score: float = 0.0
    tip_ids: list[int] = field(default_factory=list)
    catalyst: CatalystAssessment | None = None
    avg_dollar_volume: float | None = None
    reverse_split: bool = False


class Engine:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        broker: SimBroker,
        clock: mcal.Clock,
        strategies: list[Strategy] | None = None,
        learner: StrategyLearner | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.broker = broker
        self.clock = clock
        self.strategies = strategies if strategies is not None else build(cfg.agent.strategies)
        self.learner = learner or StrategyLearner(db, broker.account_id, cfg.agent)
        self.risk = RiskManager(cfg, broker, db, clock)

    # ---------------------------------------------------------------- signals
    def signals_for(self, symbol: str, df: pd.DataFrame, ctx: SignalContext) -> list[Signal]:
        out = []
        for s in self.strategies:
            if ctx.phase not in s.phases or (s.live_only and ctx.mode != "live"):
                continue
            try:
                sig = s.evaluate(symbol, df, ctx)
            except Exception as exc:  # noqa: BLE001 - one broken strategy must not stop the others
                log.warning("strategy %s failed on %s: %s", s.name, symbol, exc)
                continue
            if sig:
                out.append(sig)
        return out

    def strategy(self, name: str | None) -> Strategy | None:
        return next((s for s in self.strategies if s.name == name), None)

    # ---------------------------------------------------------------- entries
    def _closed_today(self) -> set[str]:
        today = self.clock.now().date().isoformat()
        rows = self.db.all(
            "SELECT symbol FROM trades WHERE account_id=? AND status='closed' AND substr(closed_at,1,10)=?",
            [self.broker.account_id, today],
        )
        return {r["symbol"] for r in rows}

    def decide_entries(
        self, candidates: list[EntryCandidate], equity: float, active_from: datetime | None = None
    ) -> list[dict[str, Any]]:
        names = [s.name for s in self.strategies]
        weights = self.learner.weights(names)
        u = self.cfg.universe
        held = {p["symbol"] for p in self.broker.positions()} | {
            o["symbol"] for o in self.broker.open_orders() if o["side"] == "buy"
        }
        cooldown = self._closed_today()
        rows = []
        for c in candidates:
            sig = c.signal
            w = weights.get(sig.strategy, 1.0)
            final = sig.score * w * (1 + 0.15 * min(max(c.tip_score, 0.0), 2.0))
            sid = self.db.insert(
                "signals",
                {
                    "ts": self.clock.now().isoformat(), "symbol": sig.symbol, "strategy": sig.strategy,
                    "score": sig.score, "final_score": round(final, 4), "ref_price": sig.ref_price,
                    "stop_distance": sig.stop_distance, "reward_multiple": sig.reward_multiple,
                    "data": dumps({"reason": sig.reason, "weight": w, "tip_score": c.tip_score, **sig.data}),
                },
            )
            rows.append((final, sid, c))
        rows.sort(key=lambda r: -r[0])
        decisions = []
        seen: set[str] = set()
        for final, sid, c in rows:
            sig = c.signal
            sym = sig.symbol
            decision, why = "skip", ""
            if sym in seen:
                why = "a stronger signal on this symbol was already considered"
            elif sym in held:
                why = "already holding or buying this symbol"
            elif sym in cooldown:
                why = "closed a trade in this symbol today"
            elif final <= 0:
                why = f"strategy {sig.strategy} is quarantined by the learner"
            elif c.catalyst is not None and c.catalyst.dilution_risk:
                why = "dilution risk (offering/ATM/prospectus) flagged"
            elif c.reverse_split:
                why = "reverse split in the last 90 days"
            elif not (u.min_price <= sig.ref_price <= u.max_price):
                why = f"price {sig.ref_price:.2f} outside ${u.min_price}-${u.max_price}"
            elif c.avg_dollar_volume is not None and c.avg_dollar_volume < u.min_avg_dollar_volume:
                why = f"avg dollar volume ${c.avg_dollar_volume:,.0f} below ${u.min_avg_dollar_volume:,.0f}"
            else:
                as_of = active_from.astimezone(mcal.ET).date() if active_from else None
                ok, gate = self.risk.entry_gate(equity, as_of)
                if not ok:
                    why = gate
                else:
                    size = self.risk.size(sig, c.quote, equity, as_of)
                    if not size.ok:
                        why = size.reason
                    else:
                        order = self.broker.submit_order(
                            sym, "buy", size.qty, "limit", limit_price=size.limit_price, tif="day",
                            strategy=sig.strategy, reason=sig.reason, ref_price=c.quote.last, active_from=active_from,
                            meta={
                                "stop_distance": sig.stop_distance, "reward_multiple": sig.reward_multiple,
                                "max_hold_days": sig.max_hold_days, "atr": sig.atr, "signal_id": sid,
                                "tip_ids": c.tip_ids[:20], "est_cost": size.est_cost, "est_reward": size.est_reward,
                            },
                        )
                        if order.get("status") == "rejected":
                            decision, why = "rejected", f"broker rejected: {order.get('meta')}"
                        else:
                            decision, why = "entered", size.reason
                            held.add(sym)
                            self._journal(
                                "info", "entry", sym,
                                f"BUY {size.qty} {sym} limit {size.limit_price} [{sig.strategy} {final:.2f}]: {sig.reason}",
                                {"signal_id": sid, "order_id": order["id"]},
                            )
            seen.add(sym)
            self.db.execute("UPDATE signals SET decision=?, decision_reason=? WHERE id=?", [decision, why, sid])
            decisions.append({"signal_id": sid, "symbol": sym, "strategy": sig.strategy, "score": final,
                              "decision": decision, "reason": why})
        return decisions

    # ------------------------------------------------------------- management
    def hold_days(self, trade: dict[str, Any], today: date) -> int:
        opened = datetime.fromisoformat(trade["opened_at"]).astimezone(mcal.ET).date()
        return mcal.trading_days_between(opened, today)

    def trail(self, trade: dict[str, Any], high: float, atr_now: float | None) -> None:
        """Ratchet the stop: break-even plus fees at +1R, ATR trail from +2R."""
        if not trade["entry_qty"] or trade["initial_stop"] is None:
            return
        r = self.cfg.risk
        entry = trade["entry_value"] / trade["entry_qty"]
        risk_ps = entry - trade["initial_stop"]
        if risk_ps <= 0:
            return
        hw = max(trade["high_water"] or entry, high)
        if hw != trade["high_water"]:
            self.broker.update_trade(trade["id"], {"high_water": hw})
        progress = (hw - entry) / risk_ps
        if progress >= r.breakeven_at_r:
            fees_ps = 2 * trade["fees"] / trade["entry_qty"]
            self.broker.move_stop(trade["id"], entry + fees_ps + tick_size(entry), f"+{progress:.1f}R reached, stop to break-even")
        atr = atr_now or trade["atr"]
        if progress >= r.trail_at_r and atr:
            self.broker.move_stop(trade["id"], hw - r.trail_atr_mult * atr, f"+{progress:.1f}R reached, trailing {r.trail_atr_mult} ATR")

    def exit_due(self, trade: dict[str, Any], df: pd.DataFrame | None, ctx: SignalContext, today: date) -> str | None:
        max_hold = trade["max_hold_days"] or self.cfg.risk.max_hold_days
        if self.hold_days(trade, today) >= max_hold:
            return f"time_stop ({max_hold} trading days)"
        strat = self.strategy(trade["strategy"])
        if strat is not None and df is not None:
            reason = strat.exit_reason(trade, df, ctx)
            if reason:
                return f"strategy_exit: {reason}"
        return None

    def _journal(self, level: str, event: str, symbol: str | None, message: str, data: dict | None = None) -> None:
        self.db.insert(
            "journal",
            {"ts": self.clock.now().isoformat(), "level": level, "event": event, "symbol": symbol,
             "message": message, "data": dumps(data or {})},
        )
