"""The autonomous loop.

    premarket   score yesterday's tips, scout today's, build the watchlist
    open        poll quotes, let the broker match orders, ratchet stops,
                take time stops, look for entries, re-scout periodically
    after close mark to market, close the day on the doubling scoreboard,
                write the daily summary

Run it with `stockagent run` (loops forever) or `stockagent run --once` from
cron. Each tick is idempotent: running it twice does no harm.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from . import clock as mcal
from .broker import SimBroker
from .config import Config
from .data.http import DataError
from .data.provider import MarketData, avg_dollar_volume
from .db import Database, dumps
from .engine import Engine, EntryCandidate
from .execution import Quote
from .learning import SourceLearner
from .llm import CatalystLLM
from .scout import Scout, recent_reverse_split
from .strategies import SignalContext

log = logging.getLogger(__name__)


class TradingAgent:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        md: MarketData,
        broker: SimBroker,
        clock: mcal.Clock,
        llm: CatalystLLM | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.md = md
        self.broker = broker
        self.clock = clock
        self.engine = Engine(cfg, db, broker, clock)
        self.sources = SourceLearner(db)
        self.scout = Scout(cfg, db, md, clock, llm, self.sources)
        self.candidates: list[dict[str, Any]] = []
        self._history: dict[str, tuple[date, pd.DataFrame]] = {}
        self._last_poll: dict[str, datetime] = {}
        self._last_range: dict[str, tuple[float | None, float | None, date]] = {}
        self._last_scout: datetime | None = None
        self._last_equity_snapshot: datetime | None = None

    # ------------------------------------------------------------------ loop
    def tick(self) -> str:
        now = self.clock.now()
        phase = mcal.market_phase(now)
        today = now.date()
        self.broker.expire_day_orders()
        if mcal.is_trading_day(today):
            if phase in ("premarket", "open") and self.db.get_meta("premarket_day") != today.isoformat():
                self.premarket()
            if phase == "open":
                self.intraday()
            _, close_dt = mcal.session_times(today)
            if now >= close_dt and self.db.get_meta("eod_day") != today.isoformat():
                self.end_of_day()
        return phase

    def run_forever(self) -> None:
        log.info("agent running; Ctrl-C to stop")
        while True:
            try:
                phase = self.tick()
            except DataError as exc:
                log.warning("data problem this tick: %s", exc)
                self._journal("warn", "data_error", None, str(exc)[:300])
                phase = "open"
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, record what broke
                log.exception("tick failed")
                self._journal("error", "tick_failed", None, f"{type(exc).__name__}: {exc}"[:500])
                phase = "open"
            time.sleep(self._sleep_for(phase))

    def _sleep_for(self, phase: str) -> float:
        if phase == "open":
            return self.cfg.agent.poll_seconds
        if phase == "premarket":
            return 300
        return 900

    # ------------------------------------------------------------- premarket
    def premarket(self) -> None:
        today = self.clock.now().date()
        self.db.set_meta("premarket_day", today.isoformat())
        evaluated = self.scout.evaluate_tips()
        self._refresh_marks([p["symbol"] for p in self.broker.positions()])
        equity = self.broker.equity()
        self.broker.record_equity("start", self.engine.risk.target_equity(today))
        self.engine.risk.ensure_cycle(today, equity)
        self.candidates = self.scout.run()
        self._last_scout = self.clock.now()
        pace = self.engine.risk.pace(equity, today)
        self._journal(
            "info", "premarket", None,
            f"day {pace.trading_day_index + 1}, cycle {pace.cycle_number} day {pace.cycle_day}: equity ${equity:.2f}, "
            f"cycle target ${pace.cycle_target:.2f} needs {pace.required_daily_return:+.1%}/day; "
            f"scored {evaluated} past tips; watchlist {len(self.candidates)}",
            {"pace": pace.to_dict()},
        )

    # --------------------------------------------------------------- session
    def intraday(self) -> None:
        now = self.clock.now()
        today = now.date()
        if self._last_scout is None or now - self._last_scout >= timedelta(minutes=self.cfg.agent.scout_interval_minutes):
            self._rescout()
        if not self.candidates:
            self.candidates = self._load_watchlist(today)

        held = [p["symbol"] for p in self.broker.positions()]
        pending = sorted({o["symbol"] for o in self.broker.open_orders()})
        watch = [c["symbol"] for c in self.candidates[: self.cfg.agent.watchlist_size]]
        symbols = sorted(set(held) | set(pending) | set(watch))
        if not symbols:
            return
        quotes = self.md.quotes(symbols)
        for sym in set(held) | set(pending):
            if sym in quotes:
                self._attach_range(quotes[sym])
        for q in quotes.values():
            self.broker.update_mark(q)
        for sym in pending + held:
            if sym in quotes:
                self.broker.process_quote(quotes[sym])

        self._manage_positions(quotes, now)

        since_open = mcal.minutes_since_open(now)
        to_close = mcal.minutes_to_close(now)
        r = self.cfg.risk
        if since_open >= r.no_entry_first_minutes and to_close >= r.no_entry_last_minutes:
            self._look_for_entries(quotes, now)

        if self._last_equity_snapshot is None or now - self._last_equity_snapshot >= timedelta(minutes=15):
            self.broker.record_equity("intraday", self.engine.risk.target_equity(today))
            self._last_equity_snapshot = now

    def _rescout(self) -> None:
        self.candidates = self.scout.run()
        self._last_scout = self.clock.now()
        held = [p["symbol"] for p in self.broker.positions()]
        if not held:
            return
        for sym, cat in self.scout.held_catalysts(held).items():
            if cat.score <= -2.0 or cat.dilution_risk:
                q = self.md.quotes([sym]).get(sym)
                if q:
                    self._journal("warn", "negative_catalyst", sym, f"exiting {sym}: {cat.summary}", cat.to_dict())
                    self.broker.close_position(sym, f"negative_catalyst: {', '.join(cat.tags[:3])}", q)

    def _attach_range(self, q: Quote) -> None:
        """Give the broker the low/high traded since the previous poll so a stop
        hit between polls is not missed."""
        since = self._last_poll.get(q.symbol)
        self._last_poll[q.symbol] = self.clock.now()
        prev = self._last_range.get(q.symbol)
        self._last_range[q.symbol] = (q.day_low, q.day_high, self.clock.now().date())
        try:
            bars = self.md.intraday_bars(q.symbol, "1m")
        except (DataError, NotImplementedError):
            # No minute bars (Yahoo blocked): a new session low or high since the
            # previous poll must have traded between the two polls.
            if prev and prev[2] == self.clock.now().date():
                if q.day_low is not None and prev[0] is not None and q.day_low < prev[0]:
                    q.low = min(q.day_low, q.last)
                if q.day_high is not None and prev[1] is not None and q.day_high > prev[1]:
                    q.high = max(q.day_high, q.last)
            return
        if since is not None:
            bars = bars[bars.index > since - timedelta(minutes=1)]
        if len(bars):
            q.high = max(float(bars["high"].max()), q.last)
            q.low = min(float(bars["low"].min()), q.last)

    def _manage_positions(self, quotes: dict[str, Quote], now: datetime) -> None:
        today = now.date()
        near_close = mcal.minutes_to_close(now) <= self.cfg.risk.time_stop_minutes_before_close
        for trade in self.broker.open_trades():
            sym = trade["symbol"]
            q = quotes.get(sym)
            if not q or not self.broker.position(sym):
                continue
            hist = self._daily(sym, today)
            df = self._with_today(hist, q) if hist is not None else None
            self.engine.trail(trade, q.high or q.last, self._atr(hist))
            if near_close:
                ctx = SignalContext(mode="live", phase="close", partial_bar=True, quote=q)
                reason = self.engine.exit_due(self.broker.trade(trade["id"]) or trade, df, ctx, today)
                if reason:
                    self._journal("info", "exit", sym, f"closing {sym}: {reason}", {"trade_id": trade["id"]})
                    self.broker.close_position(sym, reason, q)

    def _look_for_entries(self, quotes: dict[str, Quote], now: datetime) -> None:
        equity = self.broker.equity()
        ok, why = self.engine.risk.entry_gate(equity)
        if not ok:
            if self.db.get_meta("last_gate_reason") != why:
                self.db.set_meta("last_gate_reason", why)
                self._journal("info", "no_entries", None, f"not opening positions: {why}")
            return
        self.db.set_meta("last_gate_reason", "")
        today = now.date()
        held = {p["symbol"] for p in self.broker.positions()}
        cands: list[EntryCandidate] = []
        for c in self.candidates[: self.cfg.agent.watchlist_size]:
            sym = c["symbol"]
            q = quotes.get(sym)
            if sym in held or q is None:
                continue
            hist = self._daily(sym, today)
            if hist is None or len(hist) < 20:
                continue
            df = self._with_today(hist, q)
            ctx = SignalContext(
                mode="live", phase="intraday", partial_bar=True,
                session_fraction=mcal.session_fraction_elapsed(now), minutes_since_open=mcal.minutes_since_open(now),
                quote=q, catalyst=c.get("catalyst"), social=c.get("social"), tip_score=c.get("score", 0.0),
            )
            adv = q.avg_dollar_volume or avg_dollar_volume(hist)
            q.avg_dollar_volume = adv
            for sig in self.engine.signals_for(sym, df, ctx):
                cands.append(EntryCandidate(
                    sig, q, c.get("score", 0.0), c.get("tip_ids", []), c.get("catalyst"), adv,
                    recent_reverse_split(self.md.splits(sym), today),
                ))
        if not cands:
            return
        for d in self.engine.decide_entries(cands, equity):
            if d["decision"] == "entered":
                q = quotes.get(d["symbol"])
                if q:
                    self.broker.process_quote(q)

    # ------------------------------------------------------------ end of day
    def end_of_day(self) -> None:
        today = self.clock.now().date()
        self.db.set_meta("eod_day", today.isoformat())
        self.broker.expire_day_orders()
        self._refresh_marks([p["symbol"] for p in self.broker.positions()])
        target = self.engine.risk.target_equity(today)
        equity = self.broker.record_equity("close", target)
        self.engine.risk.close_day(today, equity)
        pace = self.engine.risk.pace(equity, today)
        start = self.engine.risk.day_start_equity(today) or equity
        trades_today = self.db.all(
            "SELECT symbol, strategy, net_pnl, exit_reason FROM trades WHERE account_id=? AND status='closed' "
            "AND substr(closed_at,1,10)=?",
            [self.broker.account_id, today.isoformat()],
        )
        fees_today = self.db.scalar(
            "SELECT COALESCE(SUM(total_fees),0) FROM fills WHERE account_id=? AND substr(ts,1,10)=?",
            [self.broker.account_id, today.isoformat()],
        )
        self._journal(
            "info", "end_of_day", None,
            f"equity ${equity:.2f} ({equity / start - 1:+.1%} today, {pace.total_multiple:.2f}x total) vs doubling path "
            f"${target:.2f} ({pace.vs_target:.0%} of path); {len(trades_today)} trades closed, fees ${fees_today:.2f}",
            {"pace": pace.to_dict(), "trades": trades_today, "strategy_stats": self.engine.learner.stats(),
             "source_weights": self.sources.weights()},
        )

    # ---------------------------------------------------------------- helpers
    def _refresh_marks(self, symbols: list[str]) -> None:
        if not symbols:
            return
        try:
            for q in self.md.quotes(symbols).values():
                self.broker.update_mark(q)
        except DataError as exc:
            self._journal("warn", "data_error", None, f"could not refresh marks: {exc}"[:300])

    def _daily(self, symbol: str, today: date) -> pd.DataFrame | None:
        cached = self._history.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        try:
            df = self.md.daily_bars(symbol, 260)
        except DataError:
            return None
        df = df[df.index.date < today]
        self._history[symbol] = (today, df)
        return df

    @staticmethod
    def _with_today(hist: pd.DataFrame, q: Quote) -> pd.DataFrame:
        o = q.open or q.last
        row = pd.DataFrame(
            {
                "open": [o],
                "high": [max(q.day_high or q.last, q.last, o)],
                "low": [min(q.day_low or q.last, q.last, o)],
                "close": [q.last],
                "volume": [float(q.volume or 0)],
            },
            index=pd.DatetimeIndex([pd.Timestamp(q.ts or datetime.now(mcal.ET)).normalize()]),
        )
        return pd.concat([hist, row])

    @staticmethod
    def _atr(hist: pd.DataFrame | None) -> float | None:
        if hist is None or len(hist) < 15:
            return None
        from .indicators import atr

        v = atr(hist).iloc[-1]
        return None if pd.isna(v) else float(v)

    def _load_watchlist(self, today: date) -> list[dict[str, Any]]:
        from .catalysts import CatalystAssessment
        from .db import loads

        out = []
        for r in self.db.all("SELECT * FROM watchlist WHERE day=? ORDER BY score DESC", [today.isoformat()]):
            data = loads(r["data"], {})
            cat = data.get("catalyst") or {}
            out.append({
                "symbol": r["symbol"], "score": data.get("tip_score", r["score"]), "final_score": r["score"],
                "tip_ids": data.get("tip_ids", []), "social": data.get("social", {}),
                "catalyst": CatalystAssessment(
                    r["symbol"], cat.get("score", 0.0), cat.get("tags", []), bool(cat.get("dilution_risk")),
                    cat.get("grader", "lexicon"), cat.get("summary", ""),
                ),
            })
        return out

    def _journal(self, level: str, event: str, symbol: str | None, message: str, data: dict | None = None) -> None:
        self.db.insert(
            "journal",
            {"ts": self.clock.now().isoformat(), "level": level, "event": event, "symbol": symbol,
             "message": message, "data": dumps(data or {})},
        )
