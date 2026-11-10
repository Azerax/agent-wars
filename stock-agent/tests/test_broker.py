from datetime import timedelta

import pytest
from conftest import at

from stockagent.broker import Bar, SimBroker
from stockagent.execution import Quote


def q(sym="ABCD", last=2.0, bid=1.99, ask=2.01, **kw):
    return Quote(sym, last, bid=bid, ask=ask, **kw)


def test_market_buy_pays_ask_plus_slippage_and_reserves_nothing_after(broker):
    o = broker.submit_order("ABCD", "buy", 20, "market", ref_price=2.0)
    assert o["status"] == "open" and o["reserved_cash"] > 40
    broker.process_quote(q())
    fill = broker.db.one("SELECT * FROM fills")
    assert fill["price"] == 2.02  # 2.01 * 1.002 rounded up to the cent
    assert broker.cash() == pytest.approx(100 - 20 * 2.02)
    assert broker.reserved_cash() == 0
    pos = broker.position("ABCD")
    assert pos["qty"] == 20 and pos["avg_price"] == 2.02


def test_sale_proceeds_are_unsettled_until_t_plus_one(broker, clk, monday):
    broker.submit_order("ABCD", "buy", 40, "limit", limit_price=2.05)
    broker.process_quote(q())
    broker.submit_order("ABCD", "sell", 40, "market")
    broker.process_quote(q(last=2.10, bid=2.09, ask=2.11))
    proceeds = broker.db.one("SELECT amount, settle_date FROM cash_ledger WHERE kind='sell'")
    assert proceeds["settle_date"] == "2026-11-03"
    # The buy was paid from settled cash; the sale proceeds are not spendable today.
    assert broker.buying_power() == pytest.approx(broker.cash() - proceeds["amount"])
    assert broker.buying_power(as_of=monday + timedelta(days=1)) == pytest.approx(broker.cash())
    clk.set(at(monday + timedelta(days=1), 10))
    assert broker.buying_power() == pytest.approx(broker.cash())


def test_insufficient_buying_power_is_rejected_and_recorded(broker):
    o = broker.submit_order("ABCD", "buy", 100, "limit", limit_price=2.0)
    assert o["status"] == "rejected"
    assert "buying power" in o["meta"]
    assert broker.db.scalar("SELECT COUNT(*) FROM journal WHERE event='order_rejected'") == 1


def test_cannot_sell_shares_not_held(broker):
    o = broker.submit_order("ABCD", "sell", 5, "market")
    assert o["status"] == "rejected"


def test_bracket_target_fill_closes_trade_net_of_fees(broker):
    broker.submit_order("ABCD", "buy", 30, "limit", limit_price=2.05, strategy="momentum_breakout",
                        meta={"stop_distance": 0.2, "reward_multiple": 2.0, "max_hold_days": 4})
    broker.process_quote(q())
    t = broker.open_trades()[0]
    assert t["initial_stop"] == 1.81 and t["target"] == pytest.approx(2.41)
    kinds = sorted(o["type"] for o in broker.open_orders("ABCD"))
    assert kinds == ["limit", "stop"]
    broker.process_quote(q(last=2.45, bid=2.44, ask=2.46))
    t = broker.trade(t["id"])
    assert t["status"] == "closed" and t["exit_reason"] == "profit_target"
    assert broker.open_orders("ABCD") == []  # the stop was cancelled as the OCO sibling
    assert t["net_pnl"] == pytest.approx(t["exit_value"] - t["entry_value"] - t["fees"])
    assert t["fees"] == pytest.approx(0.02)
    assert t["r_multiple"] == pytest.approx(t["net_pnl"] / t["initial_risk"], rel=1e-3)


def test_stop_gap_fills_below_the_stop(broker):
    broker.submit_order("ABCD", "buy", 30, "limit", limit_price=2.05,
                        meta={"stop_distance": 0.2, "reward_multiple": 2.0})
    broker.process_quote(q())
    broker.process_quote(q(last=1.50, bid=1.49, ask=1.51))  # gapped through the 1.81 stop
    exit_fill = broker.db.one("SELECT * FROM fills WHERE side='sell'")
    assert exit_fill["price"] < 1.5
    assert broker.position("ABCD") is None


def test_stop_hit_between_polls_is_seen_through_the_low(broker):
    broker.submit_order("ABCD", "buy", 30, "limit", limit_price=2.05,
                        meta={"stop_distance": 0.2, "reward_multiple": 2.0})
    broker.process_quote(q())
    broker.process_quote(q(last=2.0, low=1.78, high=2.02))
    assert broker.position("ABCD") is None


def test_bar_hitting_stop_and_target_assumes_stop_first(broker, clk, monday):
    broker.submit_order("ABCD", "buy", 30, "limit", limit_price=2.05,
                        meta={"stop_distance": 0.2, "reward_multiple": 2.0})
    broker.process_quote(q())
    clk.set(at(monday, 12))
    broker.process_bar("ABCD", Bar(monday, 2.0, 2.6, 1.7, 2.3, 5_000_000), "intraday", 5_000_000)
    t = broker.db.one("SELECT * FROM trades")
    assert t["exit_reason"] == "stop_loss"


def test_limit_entry_not_filled_when_open_gaps_above_limit(broker, clk, monday):
    nxt = monday + timedelta(days=1)
    broker.submit_order("ABCD", "buy", 20, "limit", limit_price=2.03, active_from=at(nxt, 9, 30))
    clk.set(at(nxt, 9, 30))
    broker.process_bar("ABCD", Bar(nxt, 2.40, 2.5, 2.3, 2.45, 5_000_000), "open", 5_000_000)
    broker.process_bar("ABCD", Bar(nxt, 2.40, 2.5, 2.3, 2.45, 5_000_000), "intraday", 5_000_000)
    assert broker.position("ABCD") is None


def test_orders_are_not_active_before_active_from(broker, clk, monday):
    nxt = monday + timedelta(days=1)
    broker.submit_order("ABCD", "buy", 20, "limit", limit_price=2.10, active_from=at(nxt, 9, 30))
    broker.process_quote(q())
    assert broker.position("ABCD") is None


def test_day_orders_expire_after_the_close(broker, clk, monday):
    broker.submit_order("ABCD", "buy", 10, "limit", limit_price=1.0)
    clk.set(at(monday, 16, 1))
    assert broker.expire_day_orders() == 1
    assert broker.reserved_cash() == 0


def test_good_faith_violation_is_counted(db, cfg, clk, monday):
    cfg.risk.allow_unsettled_buys = True
    b = SimBroker(db, cfg, clk, "gfv")
    b.open_account(100.0)
    b.submit_order("ABCD", "buy", 45, "limit", limit_price=2.05)
    b.process_quote(q())
    b.submit_order("ABCD", "sell", 45, "market")
    b.process_quote(q(last=2.1, bid=2.09, ask=2.11))
    # Buy again with unsettled proceeds, then sell before they settle.
    b.submit_order("EFGH", "buy", 40, "limit", limit_price=2.2)
    b.process_quote(q("EFGH", 2.1, 2.09, 2.11))
    assert b.position("EFGH")["sellable_after"] == "2026-11-03"
    b.submit_order("EFGH", "sell", 40, "market")
    b.process_quote(q("EFGH", 2.2, 2.19, 2.21))
    assert b.account()["gfv_count"] == 1


def test_move_stop_only_ratchets_up(broker):
    broker.submit_order("ABCD", "buy", 30, "limit", limit_price=2.05, meta={"stop_distance": 0.2, "reward_multiple": 2.0})
    broker.process_quote(q())
    t = broker.open_trades()[0]
    assert broker.move_stop(t["id"], 1.95, "test")
    assert not broker.move_stop(t["id"], 1.90, "lower")
    stop = [o for o in broker.open_orders("ABCD") if o["type"] == "stop"][0]
    assert stop["stop_price"] == 1.95


def test_equity_is_cash_plus_marked_positions(broker):
    broker.submit_order("ABCD", "buy", 20, "limit", limit_price=2.05)
    broker.process_quote(q())
    broker.update_mark(q(last=2.5))
    assert broker.equity() == pytest.approx(broker.cash() + 20 * 2.5)


def test_resting_target_fills_at_its_limit_not_the_better_quote(broker, clk, monday):
    broker.submit_order("ABCD", "buy", 30, "limit", limit_price=2.05,
                        meta={"stop_distance": 0.2, "reward_multiple": 2.0})
    broker.process_quote(q())
    clk.set(at(monday, 10, 1))
    broker.process_quote(q(last=2.30, bid=2.29, ask=2.31))  # target 2.41 still resting
    clk.set(at(monday, 10, 2))
    broker.process_quote(q(last=2.80, bid=2.79, ask=2.81))  # jumped through it between polls
    sell = broker.db.one("SELECT * FROM fills WHERE side='sell'")
    assert sell["price"] == 2.41


def test_new_marketable_limit_gets_the_quote(broker):
    broker.submit_order("ABCD", "buy", 10, "limit", limit_price=2.10)
    broker.process_quote(q())
    assert broker.db.one("SELECT price FROM fills")["price"] == 2.01


def test_one_venue_quote_far_wider_than_normal_is_not_used(cfg):
    from stockagent.execution import Quote as Q

    wide = Q("ABCD", 2.36, bid=2.30, ask=2.50, avg_dollar_volume=5_000_000)  # 8% wide: one exchange's quote
    bid, ask = wide.bid_ask(cfg.execution)
    assert ask - bid < 0.05 and bid < 2.36 < ask
    normal = Q("ABCD", 2.36, bid=2.35, ask=2.37, avg_dollar_volume=5_000_000)
    assert normal.bid_ask(cfg.execution) == (2.35, 2.37)
    one_sided = Q("AAPL", 350.87, bid=334.59, ask=None, avg_dollar_volume=10e9)  # the after-hours IEX quote from doctor
    b, a = one_sided.bid_ask(cfg.execution)
    assert b < 350.87 < a and a - b < 2
