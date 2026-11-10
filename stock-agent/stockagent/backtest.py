"""Replay real daily bars through the same engine, broker, fees and spread
model the live agent uses.

What a daily-bar backtest cannot tell you, stated up front:
- Only the price-based strategies run. Catalyst and social strategies need
  the headlines and chatter as they looked at the time, which free sources
  do not keep.
- Intraday order is unknown. When one bar spans both the stop and the target,
  the stop is assumed to have filled first.
- A universe picked from today's screener contains only stocks that still
  exist. Delisted losers are missing, which flatters the result.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from . import clock as mcal
from .broker import Bar, SimBroker
from .config import Config
from .data.http import DataError
from .data.provider import MarketData
from .db import Database
from .engine import Engine, EntryCandidate
from .execution import Quote
from .learning import StrategyLearner
from .report import compute_metrics
from .strategies import SignalContext, build

log = logging.getLogger(__name__)
WARMUP_DAYS = 400  # calendar days of history before `start`, for 100-day averages and squeezes


def load_history(md: MarketData, symbols: list[str], start: date, end: date) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    data, errors = {}, {}
    for sym in symbols:
        try:
            df = md.daily_history(sym, start - timedelta(days=WARMUP_DAYS), end)
        except DataError as exc:
            errors[sym] = str(exc)[:160]
            continue
        if len(df) >= 30:
            data[sym] = df
        else:
            errors[sym] = f"only {len(df)} bars"
    return data, errors


def pick_universe(md: MarketData, cfg: Config, size: int) -> list[str]:
    """Most-traded low-priced stocks right now. Survivorship-biased by construction."""
    import re

    df = md.universe().dropna(subset=["price", "volume"])
    u = cfg.universe
    rx = [re.compile(p) for p in u.exclude_symbol_patterns]
    df = df[(df["price"] >= u.min_price) & (df["price"] <= u.max_price) & (df["volume"] >= u.min_volume)]
    df = df[~df["symbol"].apply(lambda s: any(r.search(s) for r in rx))]
    df = df.assign(dv=df["price"] * df["volume"]).sort_values("dv", ascending=False)
    return list(df["symbol"].head(size))


class Backtester:
    def __init__(self, cfg: Config, data: dict[str, pd.DataFrame], start: date, end: date, seed: int = 7):
        self.cfg = cfg
        self.start, self.end = start, end
        self.data = {s: df.sort_index() for s, df in data.items() if len(df)}
        self.db = Database(":memory:")
        first = next((d for d in mcal.trading_days_in_range(start, end)), start)
        self.clock = mcal.SimClock(datetime.combine(first, time(9, 0), mcal.ET))
        self.broker = SimBroker(self.db, cfg, self.clock, "backtest")
        self.broker.open_account(cfg.risk.starting_cash)
        strategies = [s for s in build(cfg.agent.strategies) if not s.live_only]
        learner = StrategyLearner(self.db, self.broker.account_id, cfg.agent, seed=seed)
        self.engine = Engine(cfg, self.db, self.broker, self.clock, strategies, learner)
        self.pos: dict[str, dict[date, int]] = {
            s: {ts.date(): i for i, ts in enumerate(df.index)} for s, df in self.data.items()
        }
        self.adv: dict[str, pd.Series] = {
            s: (df["close"] * df["volume"]).rolling(20, min_periods=5).mean().shift(1) for s, df in self.data.items()
        }

    def _at(self, sym: str, d: date) -> int | None:
        return self.pos[sym].get(d)

    def _bar(self, sym: str, i: int) -> Bar:
        r = self.data[sym].iloc[i]
        return Bar(self.data[sym].index[i].date(), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))

    def _adv(self, sym: str, i: int) -> float | None:
        v = self.adv[sym].iloc[i]
        return None if pd.isna(v) else float(v)

    def _at_time(self, d: date, hh: int, mm: int) -> datetime:
        return datetime.combine(d, time(hh, mm), mcal.ET)

    def run(self, progress: bool = False) -> dict[str, Any]:
        days = [d for d in mcal.trading_days_in_range(self.start, self.end) if any(d in p for p in self.pos.values())]
        for n, d in enumerate(days):
            self._day(d)
            if progress and n % 20 == 0:
                log.info("%s equity %.2f", d, self.broker.equity())
        return self.results()

    def _day(self, d: date) -> None:
        u = self.cfg.universe
        risk = self.engine.risk
        active = {s: i for s in self.data if (i := self._at(s, d)) is not None}

        self.clock.set(self._at_time(d, 9, 25))
        self.broker.expire_day_orders()
        equity = self.broker.equity()
        self.broker.record_equity("start", risk.target_equity(d))
        risk.ensure_cycle(d, equity)

        # Opening auction: gap strategies see only today's open.
        self.clock.set(self._at_time(d, 9, 30))
        cands = []
        for sym, i in active.items():
            if i < 25:
                continue
            bar = self._bar(sym, i)
            if not (u.min_price <= bar.open <= u.max_price):
                continue
            hist = self.data[sym].iloc[max(0, i - 260) : i]
            row = pd.DataFrame({"open": [bar.open], "high": [bar.open], "low": [bar.open], "close": [bar.open], "volume": [0.0]},
                               index=[self.data[sym].index[i]])
            df = pd.concat([hist, row])
            ctx = SignalContext(mode="backtest", phase="open", partial_bar=True, session_fraction=0.0)
            adv = self._adv(sym, i)
            for sig in self.engine.signals_for(sym, df, ctx):
                cands.append(EntryCandidate(sig, Quote(sym, bar.open, ts=self.clock.now(), avg_dollar_volume=adv), avg_dollar_volume=adv))
        if cands:
            self.engine.decide_entries(cands, equity)

        busy = lambda: {p["symbol"] for p in self.broker.positions()} | {o["symbol"] for o in self.broker.open_orders()}  # noqa: E731
        for sym in sorted(busy()):
            if sym in active:
                self.broker.process_bar(sym, self._bar(sym, active[sym]), "open", self._adv(sym, active[sym]))

        self.clock.set(self._at_time(d, 12, 0))
        for sym in sorted(busy()):
            if sym in active:
                self.broker.process_bar(sym, self._bar(sym, active[sym]), "intraday", self._adv(sym, active[sym]))

        # Near the close: ratchet stops for tomorrow, take time stops and strategy exits.
        self.clock.set(self._at_time(d, 15, 50))
        for trade in self.broker.open_trades():
            sym = trade["symbol"]
            if sym not in active or not self.broker.position(sym):
                continue
            i = active[sym]
            bar = self._bar(sym, i)
            hist = self.data[sym].iloc[max(0, i - 260) : i + 1]
            from .indicators import atr

            a = atr(hist).iloc[-1] if len(hist) > 15 else None
            self.engine.trail(trade, bar.high, None if a is None or pd.isna(a) else float(a))
            ctx = SignalContext(mode="backtest", phase="close")
            reason = self.engine.exit_due(self.broker.trade(trade["id"]) or trade, hist, ctx, d)
            if reason:
                self.broker.close_position(sym, reason, order_type="moc")
        for sym in sorted(busy()):
            if sym in active:
                self.broker.process_bar(sym, self._bar(sym, active[sym]), "close", self._adv(sym, active[sym]))

        self.clock.set(self._at_time(d, 16, 0))
        for sym, i in active.items():
            bar = self._bar(sym, i)
            self.broker.update_mark(Quote(sym, bar.close, ts=self.clock.now()))
        equity = self.broker.record_equity("close", risk.target_equity(d))
        risk.close_day(d, equity)
        self.broker.expire_day_orders()

        # After the close: today's complete bars produce tomorrow's entry orders.
        nd = mcal.next_trading_day(d)
        tomorrow_open = self._at_time(nd, 9, 30)
        cands = []
        for sym, i in active.items():
            if i < 25:
                continue
            bar = self._bar(sym, i)
            if not (u.min_price <= bar.close <= u.max_price):
                continue
            adv = self._adv(sym, i)
            today_dv = bar.close * bar.volume
            liq = max(adv or 0.0, today_dv)
            if liq < u.min_avg_dollar_volume:
                continue
            df = self.data[sym].iloc[max(0, i - 260) : i + 1]
            ctx = SignalContext(mode="backtest", phase="close")
            for sig in self.engine.signals_for(sym, df, ctx):
                cands.append(EntryCandidate(
                    sig, Quote(sym, bar.close, ts=self.clock.now(), avg_dollar_volume=liq),
                    avg_dollar_volume=adv, dollar_volume_today=today_dv,
                ))
        if cands:
            self.engine.decide_entries(cands, equity, active_from=tomorrow_open)

    def results(self) -> dict[str, Any]:
        aid = self.broker.account_id
        closes = [(r["day"], r["equity"]) for r in self.db.all(
            "SELECT day, equity FROM equity WHERE account_id=? AND kind='close' ORDER BY id", [aid])]
        trades = self.db.all("SELECT * FROM trades WHERE account_id=? ORDER BY id", [aid])
        metrics = compute_metrics(closes, trades, self.cfg.risk.starting_cash, self.cfg.target.multiple,
                                  self.cfg.target.period_trading_days)
        cycles = self.db.all("SELECT * FROM cycles WHERE account_id=? ORDER BY number", [aid])
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": sorted(self.data),
            "metrics": metrics,
            "equity": closes,
            "trades": trades,
            "cycles": cycles,
            "signals": self.db.scalar("SELECT COUNT(*) FROM signals"),
            "signal_decisions": self.db.all("SELECT decision, COUNT(*) AS n FROM signals GROUP BY decision"),
            "strategy_weights": self.engine.learner.weights([s.name for s in self.engine.strategies], sample=False),
        }
