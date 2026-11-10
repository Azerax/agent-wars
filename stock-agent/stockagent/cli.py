"""Command line: stockagent <command>."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from . import clock as mcal
from .broker import SimBroker
from .config import load_config
from .db import Database


def _setup(args):
    cfg = load_config(args.config)
    db = Database(cfg.data.db_path)
    clk = mcal.Clock()
    broker = SimBroker(db, cfg, clk, args.account)
    return cfg, db, clk, broker


def _market(cfg):
    from .data.provider import make_market_data

    return make_market_data(cfg)


def cmd_init(args) -> int:
    cfg, db, clk, broker = _setup(args)
    if broker.account_id is not None and not args.force:
        print(f"account '{args.account}' already exists in {cfg.data.db_path}; use `reset` to start over")
        return 1
    if args.force and broker.account_id is not None:
        broker.reset(args.cash)
    else:
        broker.open_account(args.cash)
    acct = broker.account()
    print(f"opened paper account '{acct['name']}' with ${acct['starting_cash']:.2f}, first trading day {acct['start_date']}")
    print(f"database: {cfg.data.db_path}")
    return 0


def cmd_reset(args) -> int:
    cfg, db, clk, broker = _setup(args)
    broker.reset(args.cash)
    if args.all:
        for table in ("tips", "watchlist", "signals", "catalysts", "journal", "marks", "meta"):
            db.execute(f"DELETE FROM {table}")
    print(f"reset '{args.account}' to ${broker.account()['starting_cash']:.2f}")
    return 0


def _keep_windows_awake() -> bool:
    """Ask Windows not to sleep while the agent runs (the display may still
    turn off). The request ends when the process exits."""
    if sys.platform != "win32":
        return False
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required))


def cmd_run(args) -> int:
    from .agent import TradingAgent
    from .llm import CatalystLLM

    cfg, db, clk, broker = _setup(args)
    if broker.account_id is None:
        broker.open_account(cfg.risk.starting_cash)
        print(f"opened paper account with ${cfg.risk.starting_cash:.2f}")
    llm = CatalystLLM(cfg.llm)
    print(f"catalyst grader: {'Claude (' + cfg.llm.model + ')' if llm.enabled else 'lexicon only — ' + str(llm.disabled_reason)}")
    agent = TradingAgent(cfg, db, _market(cfg), broker, clk, llm)
    if args.once:
        phase = agent.tick()
        print(f"tick done (market {phase})")
        return 0
    if not args.allow_sleep and _keep_windows_awake():
        print("keeping Windows awake while the agent runs (the screen can still turn off; closing a laptop lid may still sleep it)")
    print("agent running; Ctrl-C to stop. Check on it with `python -m stockagent journal` in another window.")
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_scout(args) -> int:
    from .llm import CatalystLLM
    from .scout import Scout

    cfg, db, clk, broker = _setup(args)
    scout = Scout(cfg, db, _market(cfg), clk, CatalystLLM(cfg.llm))
    if args.evaluate:
        print(f"scored {scout.evaluate_tips()} past tips")
    cands = scout.run()
    if scout.source_errors:
        print("sources unavailable:")
        for k, v in scout.source_errors.items():
            print(f"  {k}: {v[:140]}")
    if not cands:
        print("no eligible candidates found")
        return 0
    print(f"{'symbol':<7}{'score':>7}{'catalyst':>9}  sources / why")
    for c in cands:
        cat = c["catalyst"]
        flag = "  DILUTION RISK" if cat.dilution_risk else ""
        print(f"{c['symbol']:<7}{c['final_score']:>7.2f}{cat.score:>+9.1f}  {', '.join(sorted(c['sources']))}{flag}")
        if cat.summary:
            print(f"{'':<23}{cat.summary[:110]}")
    return 0


def cmd_report(args) -> int:
    from .report import build_state, text_report

    cfg, db, clk, broker = _setup(args)
    if broker.account_id is None:
        print("no account yet; run `stockagent init`")
        return 1
    state = build_state(cfg, db, broker, clk)
    if args.json:
        print(json.dumps(state, default=str, indent=2))
    else:
        print(text_report(state))
    return 0


def cmd_backtest(args) -> int:
    from .backtest import Backtester, load_history, pick_universe

    cfg = load_config(args.config)
    md = _market(cfg)
    end = date.fromisoformat(args.end) if args.end else mcal.last_trading_day_on_or_before(date.today() - timedelta(days=1))
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=120)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else pick_universe(md, cfg, args.universe)
    print(f"loading {len(symbols)} symbols {start} .. {end}")
    data, errors = load_history(md, symbols, start, end)
    if errors:
        print(f"  skipped {len(errors)}: " + ", ".join(f"{k} ({v[:40]})" for k, v in list(errors.items())[:8]))
    if not data:
        print("no data loaded; check `stockagent doctor`")
        return 1
    res = Backtester(cfg, data, start, end, seed=args.seed).run()
    m = res["metrics"]
    print(f"\n{len(res['symbols'])} symbols, {m['trading_days']} trading days, {m['trades']} trades")
    print(f"  ${m['starting_equity']:.2f} -> ${m['final_equity']:.2f} ({m['total_return']:+.1%}), max drawdown {m['max_drawdown']:.0%}")
    print(f"  win rate {m['win_rate']}, avg R {m['avg_r']}, profit factor {m['profit_factor']}")
    print(f"  fees ${m['fees_paid']:.2f}, spread+slippage ~${m['spread_cost']:.2f}")
    print(f"  4-day windows: {m['windows']}, doubled in {m['windows_doubled']}, best {m['best_window_multiple']}x, "
          f"median {m['median_window_multiple']}x")
    print(f"  the doubling path would have reached ${m['doubling_path_final']:,.2f}")
    for name, s in m["by_strategy"].items():
        print(f"  {name:<18} {s['trades']:>3} trades, {s['wins']} wins, net ${s['net_pnl']:+.2f}, fees ${s['fees']:.2f}")
    print("\nNot simulated: catalyst/social strategies (no point-in-time news), intraday order within a bar "
          "(stop assumed first), delisted stocks (universe is today's survivors).")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(res, fh, default=str, indent=2)
        print(f"full results: {args.out}")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    cfg, db, clk, broker = _setup(args)
    if broker.account_id is None:
        broker.open_account(cfg.risk.starting_cash)
    serve(cfg, db, broker, clk, args.host, args.port, _market(cfg) if args.live_quotes else None)
    return 0


def cmd_doctor(args) -> int:
    from .config import find_alpaca_keys, find_alphavantage_key
    from .data.http import DataError, HttpClient
    from .data.provider import AlpacaMarketData, check_sources, make_market_data
    from .llm import CatalystLLM

    cfg = load_config(args.config)
    http = HttpClient(cfg.data.cache_path, cfg.data.user_agent, timeout=10.0, min_interval=0.2, retries=1)
    try:
        md = make_market_data(cfg, http)
    except DataError as exc:
        print(f"cannot start: {exc}")
        return 2
    _k, _s, alpaca_where = find_alpaca_keys(cfg.data.alpaca_key_file)
    _key, where = find_alphavantage_key("", cfg.data.alphavantage_key_file)
    found = where if cfg.data.alphavantage_api_key else "not found (set ALPHA, or put ALPHA=yourkey in .alpha)"
    using = "Alpaca (licensed API)" if isinstance(md, AlpacaMarketData) else "Yahoo + Nasdaq web endpoints (legacy)"
    print(f"price data: {using}   [data.provider = {cfg.data.provider}]")
    print(f"alpaca keys: {alpaca_where}")
    print(f"alpha vantage key: {found}")
    print("data sources:", flush=True)

    def show(name: str, role: str, ok: bool, detail: str) -> None:
        print(f"  {'OK  ' if ok else 'FAIL'} {role:<9}{name:<24} {detail}", flush=True)

    results = check_sources(md, show)
    ok_by_role: dict[str, list[str]] = {}
    for name, role, ok, _ in results:
        if ok:
            ok_by_role.setdefault(role, []).append(name)
    llm = CatalystLLM(cfg.llm)
    print(f"claude catalyst grader: {'enabled' if llm.enabled else 'off (' + str(llm.disabled_reason) + ')'}")
    if not cfg.data.sec_user_agent:
        print("note: SEC filings need STOCKAGENT_SEC_USER_AGENT='Your Name you@example.com'")
    now = mcal.Clock().now()
    print(f"market: {mcal.market_phase(now)} ({now:%Y-%m-%d %H:%M} ET)")
    prices = ok_by_role.get("prices", [])
    has_history = any(n in prices for n in ("yahoo chart", "nasdaq daily history", "alpaca daily bars (SIP)"))
    has_quotes = any(n in prices for n in ("yahoo chart", "yahoo quote (bid/ask)", "nasdaq quote", "alpaca quote (IEX)"))
    print()
    if has_history and has_quotes and ok_by_role.get("universe"):
        print(f"READY: prices via {', '.join(prices)}; tips from {len(ok_by_role.get('tips', []))} sources")
        return 0
    print("NOT READY: the agent needs daily history, live quotes and the list of listed stocks to trade")
    return 2


def cmd_journal(args) -> int:
    """What the agent has been doing, condensed: is it alive, is it finding
    tips, are strategies firing, and why are signals being skipped."""
    import re
    from datetime import datetime

    cfg, db, clk, broker = _setup(args)
    since = (clk.now() - timedelta(days=args.days)).date().isoformat()
    last = db.one("SELECT ts, event, message FROM journal ORDER BY id DESC LIMIT 1")
    last_tick = db.get_meta("last_tick")
    now = clk.now()

    def ago(ts: str | None) -> str:
        if not ts:
            return "never"
        mins = (now - datetime.fromisoformat(ts)).total_seconds() / 60
        return f"{ts[:16].replace('T', ' ')} ({mins:.0f} min ago)" if mins < 600 else ts[:16].replace("T", " ")

    print(f"database: {cfg.data.db_path}")
    print(f"agent last ticked: {ago(last_tick)}   (it ticks every {cfg.agent.poll_seconds}s in market hours, every 5-15 min otherwise)")
    print(f"last journal entry: {ago(last['ts'] if last else None)}")
    tips = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(eligible),0) AS ok, COUNT(DISTINCT symbol) AS syms FROM tips WHERE day>=?", [since])
    print(f"tips since {since}: {tips['n']} ({tips['ok']} eligible, {tips['syms']} symbols)")
    by_src = db.all("SELECT source, COUNT(*) AS n FROM tips WHERE day>=? GROUP BY source ORDER BY n DESC", [since])
    if by_src:
        print("  by source: " + ", ".join(f"{r['source']} {r['n']}" for r in by_src))
    rejects = db.all(
        "SELECT reject_reason AS r FROM tips WHERE day>=? AND eligible=0 AND reject_reason IS NOT NULL", [since]
    )
    if rejects:
        counts: dict[str, int] = {}
        for r in rejects:
            key = re.sub(r"[\d.,]+", "#", r["r"])
            counts[key] = counts.get(key, 0) + 1
        print("  rejected tips: " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:5]))
    wl = db.all("SELECT symbol, score FROM watchlist WHERE day=(SELECT MAX(day) FROM watchlist) ORDER BY score DESC")
    print(f"watchlist ({len(wl)}): " + ", ".join(f"{w['symbol']} {w['score']:.2f}" for w in wl[:15]))
    sigs = db.all("SELECT strategy, decision, decision_reason FROM signals WHERE ts>=?", [since])
    print(f"signals since {since}: {len(sigs)}")
    if sigs:
        by_strat: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for g in sigs:
            by_strat[g["strategy"]] = by_strat.get(g["strategy"], 0) + 1
            key = f"{g['decision']}: " + re.sub(r"\$?[\d.,]+%?", "#", g["decision_reason"] or "")
            reasons[key] = reasons.get(key, 0) + 1
        print("  by strategy: " + ", ".join(f"{k} {v}" for k, v in sorted(by_strat.items(), key=lambda kv: -kv[1])))
        print("  decisions:")
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {v:>4}  {k[:110]}")
    probs = db.all(
        "SELECT ts, level, event, message FROM journal WHERE level IN ('warn','error') AND ts>=? ORDER BY id DESC LIMIT 8",
        [since],
    )
    if probs:
        print("recent warnings/errors:")
        for j in probs:
            print(f"  {j['ts'][5:16].replace('T', ' ')} {j['event']}: {j['message'][:140]}")
    print(f"\nlast {args.limit} journal entries:")
    for j in reversed(db.all("SELECT ts, event, symbol, message FROM journal ORDER BY id DESC LIMIT ?", [args.limit])):
        print(f"  {j['ts'][5:16].replace('T', ' ')} {j['event']:<14} {(j['symbol'] or ''):<6} {j['message'][:120]}")
    return 0


def cmd_order(args) -> int:
    cfg, db, clk, broker = _setup(args)
    o = broker.submit_order(args.symbol, args.side, args.qty, args.type, limit_price=args.limit, stop_price=args.stop,
                            tif=args.tif, strategy="manual", reason="manual order")
    print(json.dumps(o, default=str, indent=2))
    return 0 if o.get("status") != "rejected" else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stockagent", description="Autonomous low-priced-stock paper-trading agent.")
    p.add_argument("--config", help="TOML config file (default: ./config.toml if present)")
    p.add_argument("--account", default="paper", help="paper account name")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="open the paper account")
    s.add_argument("--cash", type=float, default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("reset", help="wipe the account back to starting cash")
    s.add_argument("--cash", type=float, default=None)
    s.add_argument("--all", action="store_true", help="also forget tips, journal and learned source stats")
    s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("run", help="run the autonomous agent")
    s.add_argument("--once", action="store_true", help="one tick then exit (for cron)")
    s.add_argument("--allow-sleep", action="store_true", help="do not ask Windows to stay awake")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("scout", help="find and rank tips now")
    s.add_argument("--evaluate", action="store_true", help="also score past tips first")
    s.set_defaults(fn=cmd_scout)

    s = sub.add_parser("report", help="account, target pace, strategies, sources")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("backtest", help="replay real daily bars through the engine")
    s.add_argument("--symbols", help="comma-separated; default: most-traded low-priced stocks today")
    s.add_argument("--universe", type=int, default=40, help="how many symbols to pick when --symbols is not given")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", help="write full results JSON here")
    s.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("serve", help="dashboard and JSON API on localhost")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--live-quotes", action="store_true", help="fill manual orders against live quotes immediately")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("doctor", help="check which data sources are reachable")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("journal", help="what the agent has been doing, and why it did or did not trade")
    s.add_argument("--days", type=int, default=1, help="look back this many days")
    s.add_argument("--limit", type=int, default=25, help="recent journal entries to show")
    s.set_defaults(fn=cmd_journal)

    s = sub.add_parser("order", help="place a manual paper order (filled by the agent's next tick)")
    s.add_argument("side", choices=["buy", "sell"])
    s.add_argument("qty", type=int)
    s.add_argument("symbol")
    s.add_argument("--type", default="limit", choices=["market", "limit", "stop"])
    s.add_argument("--limit", type=float)
    s.add_argument("--stop", type=float)
    s.add_argument("--tif", default="day", choices=["day", "gtc"])
    s.set_defaults(fn=cmd_order)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
