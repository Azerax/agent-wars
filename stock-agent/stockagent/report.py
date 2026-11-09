"""Performance metrics and the state the dashboard and CLI report show."""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from . import clock as mcal
from .broker import SimBroker
from .config import Config
from .db import Database, loads
from .learning import SourceLearner, StrategyLearner
from .risk import RiskManager


def compute_metrics(
    closes: list[tuple[str, float]],
    trades: list[dict[str, Any]],
    starting_cash: float,
    multiple: float = 2.0,
    period: int = 4,
) -> dict[str, Any]:
    """`closes` is one (day, equity) per trading day, oldest first."""
    eq = [starting_cash] + [e for _, e in closes]
    final = eq[-1]
    peak, max_dd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, 1 - e / peak)
    windows = [eq[i + period] / eq[i] for i in range(len(eq) - period) if eq[i] > 0]
    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if (t.get("net_pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("net_pnl") or 0) <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] or 0 for t in losses)
    rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    n_days = len(closes)
    target_final = starting_cash * multiple ** (n_days / period) if n_days else starting_cash
    log_progress = (
        math.log(final / starting_cash) / math.log(target_final / starting_cash)
        if final > 0 and target_final > starting_cash
        else None
    )
    by_strategy: dict[str, dict[str, Any]] = {}
    for t in closed:
        s = by_strategy.setdefault(t.get("strategy") or "manual", {"trades": 0, "wins": 0, "net_pnl": 0.0, "fees": 0.0})
        s["trades"] += 1
        s["wins"] += int((t.get("net_pnl") or 0) > 0)
        s["net_pnl"] = round(s["net_pnl"] + (t.get("net_pnl") or 0), 4)
        s["fees"] = round(s["fees"] + (t.get("fees") or 0), 4)
    return {
        "trading_days": n_days,
        "starting_equity": starting_cash,
        "final_equity": round(final, 4),
        "total_return": round(final / starting_cash - 1, 4),
        "max_drawdown": round(max_dd, 4),
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "net_pnl": round(sum(t.get("net_pnl") or 0 for t in closed), 4),
        "fees_paid": round(sum(t.get("fees") or 0 for t in trades), 4),
        "spread_cost": round(sum(t.get("spread_cost") or 0 for t in trades), 4),
        "best_trade": round(max((t["net_pnl"] for t in closed), default=0.0), 4),
        "worst_trade": round(min((t["net_pnl"] for t in closed), default=0.0), 4),
        "windows": len(windows),
        "windows_doubled": sum(1 for w in windows if w >= multiple),
        "best_window_multiple": round(max(windows), 4) if windows else None,
        "median_window_multiple": round(sorted(windows)[len(windows) // 2], 4) if windows else None,
        "doubling_path_final": round(target_final, 2),
        "share_of_target_path_log": round(log_progress, 4) if log_progress is not None else None,
        "by_strategy": by_strategy,
    }


def build_state(cfg: Config, db: Database, broker: SimBroker, clock: mcal.Clock) -> dict[str, Any]:
    acct = broker.account()
    marks = broker.marks()
    equity = broker.equity(marks)
    risk = RiskManager(cfg, broker, db, clock)
    today = clock.now().date()
    pace = risk.pace(equity, today).to_dict() if acct else None
    positions = []
    for p in broker.positions():
        t = broker.trade(p["trade_id"]) or {}
        last = marks.get(p["symbol"], p["avg_price"])
        positions.append({
            **p, "last": last, "market_value": round(p["qty"] * last, 4),
            "unrealized": round(p["qty"] * last - p["cost_basis"], 4), "strategy": t.get("strategy"),
            "stop": t.get("current_stop"), "target": t.get("target"),
            "hold_days": mcal.trading_days_between(date.fromisoformat(p["opened_at"][:10]), today),
        })
    aid = broker.account_id
    closes = [(r["day"], r["equity"]) for r in db.all(
        "SELECT day, equity FROM equity WHERE account_id=? AND kind='close' ORDER BY id", [aid])]
    curve = db.all(
        "SELECT ts, day, kind, equity, target_equity FROM equity WHERE account_id=? ORDER BY id", [aid])
    trades = db.all("SELECT * FROM trades WHERE account_id=? ORDER BY id DESC LIMIT 200", [aid])
    all_trades = db.all("SELECT * FROM trades WHERE account_id=?", [aid])
    metrics = compute_metrics(closes, all_trades, acct["starting_cash"], cfg.target.multiple, cfg.target.period_trading_days)
    return {
        "as_of": clock.now().isoformat(),
        "phase": mcal.market_phase(clock.now()),
        "account": {
            **acct, "equity": round(equity, 4), "cash": round(broker.cash(), 4),
            "settled_cash": round(broker.settled_cash(), 4), "buying_power": round(broker.buying_power(), 4),
            "positions_value": round(broker.positions_value(marks), 4),
        },
        "target": {"multiple": cfg.target.multiple, "period_trading_days": cfg.target.period_trading_days},
        "pace": pace,
        "metrics": metrics,
        "positions": positions,
        "orders": db.all("SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT 100", [aid]),
        "trades": trades,
        "equity_curve": curve,
        "cycles": db.all("SELECT * FROM cycles WHERE account_id=? ORDER BY number", [aid]),
        "watchlist": [
            {**r, "sources": loads(r["sources"], []), "data": loads(r["data"], {})}
            for r in db.all("SELECT * FROM watchlist WHERE day=(SELECT MAX(day) FROM watchlist) ORDER BY score DESC LIMIT 40")
        ],
        "tips": db.all("SELECT id, ts, day, symbol, source, kind, title, url, raw_score, price_at_tip, eligible, "
                       "reject_reason, fwd_1d, fwd_4d FROM tips ORDER BY id DESC LIMIT 300"),
        "signals": db.all("SELECT * FROM signals ORDER BY id DESC LIMIT 150"),
        "source_stats": SourceLearner(db).stats(),
        "source_weights": SourceLearner(db).weights(),
        "strategy_stats": StrategyLearner(db, aid, cfg.agent).stats(),
        "strategy_weights": StrategyLearner(db, aid, cfg.agent).weights([*cfg.agent.strategies], sample=False),
        "journal": db.all("SELECT * FROM journal ORDER BY id DESC LIMIT 250"),
        "fees": cfg.fees.__dict__,
    }


def text_report(state: dict[str, Any]) -> str:
    a, p, m = state["account"], state["pace"] or {}, state["metrics"]
    t = state["target"]
    lines = [
        f"Paper account '{a['name']}' since {a['start_date']}  (as of {state['as_of'][:16]}, market {state['phase']})",
        f"  equity ${a['equity']:.2f}  cash ${a['cash']:.2f}  settled ${a['settled_cash']:.2f}  "
        f"buying power ${a['buying_power']:.2f}  positions ${a['positions_value']:.2f}",
        f"  {p.get('total_multiple', 0):.2f}x starting ${a['starting_cash']:.2f}; max drawdown {m['max_drawdown']:.0%}",
        "",
        f"Target: {t['multiple']:g}x every {t['period_trading_days']} trading days",
        f"  doubling path today ${p.get('target_today', 0):,.2f}; equity is {p.get('vs_target', 0):.1%} of it",
        f"  cycle {p.get('cycle_number')} day {p.get('cycle_day')}: ${p.get('cycle_start_equity', 0):.2f} -> "
        f"target ${p.get('cycle_target', 0):.2f}, now {p.get('cycle_multiple', 0):.2f}x, needs "
        f"{p.get('required_daily_return', 0):+.1%}/day for the rest of the cycle",
    ]
    done = [c for c in state["cycles"] if c["multiple"] is not None]
    if done:
        hits = sum(c["hit"] for c in done)
        lines.append(f"  completed cycles: {len(done)}, doubled in {hits}; multiples "
                     + ", ".join(f"{c['multiple']:.2f}x" for c in done[-8:]))
    lines += [
        "",
        f"Trades: {m['trades']} closed, win rate {m['win_rate'] if m['win_rate'] is not None else '-'}, "
        f"avg R {m['avg_r'] if m['avg_r'] is not None else '-'}, profit factor {m['profit_factor'] or '-'}",
        f"  net P&L ${m['net_pnl']:+.2f} after ${m['fees_paid']:.2f} fees and ~${m['spread_cost']:.2f} spread/slippage",
    ]
    if state["positions"]:
        lines.append("")
        lines.append("Open positions:")
        for pos in state["positions"]:
            lines.append(
                f"  {pos['symbol']:<6} {pos['qty']:>6} @ {pos['avg_price']:.4f}  last {pos['last']:.4f}  "
                f"P&L ${pos['unrealized']:+.2f}  stop {pos['stop']}  target {pos['target']}  "
                f"[{pos['strategy']}, day {pos['hold_days']}]"
            )
    if state["strategy_stats"]:
        lines.append("")
        lines.append("Strategies (learned weight):")
        for name, s in sorted(state["strategy_stats"].items()):
            w = state["strategy_weights"].get(name)
            lines.append(
                f"  {name:<18} {s['trades']:>3} trades  win {s['win_rate'] if s['win_rate'] is not None else '-'}  "
                f"avg R {s['avg_r'] if s['avg_r'] is not None else '-'}  net ${s['net_pnl']:+.2f}  "
                f"weight {w}{'  QUARANTINED' if s['quarantined'] else ''}"
            )
    if state["source_stats"]:
        lines.append("")
        lines.append("Tip sources (what their tips did next):")
        for src, s in sorted(state["source_stats"].items(), key=lambda kv: -(kv[1]["tips"])):
            avg1 = f"{s['avg_fwd_1d']:+.1%}" if s["avg_fwd_1d"] is not None else "-"
            avg4 = f"{s['avg_fwd_4d']:+.1%}" if s["avg_fwd_4d"] is not None else "-"
            lines.append(
                f"  {src:<28} {s['tips']:>5} tips  1d {avg1:>7} (n={s['evaluated_1d']})  4d {avg4:>7} "
                f"(n={s['evaluated_4d']}, {s['doubled_in_4d']} doubled)  weight {state['source_weights'].get(src, 1.0)}"
            )
    if state["watchlist"]:
        lines.append("")
        lines.append("Watchlist: " + ", ".join(f"{w['symbol']} ({w['score']:.2f})" for w in state["watchlist"][:12]))
    return "\n".join(lines)
