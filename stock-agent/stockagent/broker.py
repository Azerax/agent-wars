"""The simulated broker and exchange.

A US cash account: no margin, no shorting, T+1 settlement, whole shares.
Orders are matched against real quotes (live) or real daily bars (backtest)
with the spread, slippage and fees a $100 account would actually pay.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from . import clock as mcal
from .config import Config
from .db import Database, dumps, loads
from .execution import (
    Quote,
    buy_price,
    estimate_spread,
    round_to_tick,
    sell_price,
    tick_size,
)
from .fees import compute_fees

OPEN_STATUSES = ("open", "partial")
ORDER_TYPES = ("market", "limit", "stop", "moc")
# Within one symbol, check protective stops before profit targets: when one
# bar could have hit both, assume the worse outcome happened first.
_TYPE_PRIORITY = {"market": 0, "moc": 0, "stop": 1, "limit": 2}


class BrokerError(Exception):
    pass


@dataclass
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


class SimBroker:
    def __init__(self, db: Database, cfg: Config, clock: mcal.Clock, account_name: str = "paper"):
        self.db = db
        self.cfg = cfg
        self.clock = clock
        self.account_name = account_name
        row = db.one("SELECT * FROM accounts WHERE name=?", [account_name])
        self.account_id: int | None = row["id"] if row else None

    # ------------------------------------------------------------------ account
    def _now(self) -> str:
        return self.clock.now().isoformat()

    def _today(self) -> date:
        return self.clock.now().astimezone(mcal.ET).date()

    def open_account(self, starting_cash: float | None = None) -> int:
        if self.account_id is not None:
            return self.account_id
        cash = float(starting_cash if starting_cash is not None else self.cfg.risk.starting_cash)
        today = self._today()
        start_day = today if mcal.is_trading_day(today) else mcal.next_trading_day(today)
        self.account_id = self.db.insert(
            "accounts",
            {
                "name": self.account_name,
                "created_at": self._now(),
                "starting_cash": cash,
                "start_date": start_day.isoformat(),
                "peak_equity": cash,
            },
        )
        self.db.insert(
            "cash_ledger",
            {
                "account_id": self.account_id,
                "ts": self._now(),
                "trade_date": today.isoformat(),
                "settle_date": today.isoformat(),
                "amount": cash,
                "kind": "deposit",
                "note": "starting cash",
            },
        )
        return self.account_id

    def reset(self, starting_cash: float | None = None) -> int:
        if self.account_id is not None:
            aid = self.account_id
            with self.db.transaction() as db:
                for table in ("cash_ledger", "orders", "fills", "positions", "trades", "equity", "cycles"):
                    db.execute(f"DELETE FROM {table} WHERE account_id=?", [aid])
                db.execute("DELETE FROM accounts WHERE id=?", [aid])
            self.account_id = None
        return self.open_account(starting_cash)

    def account(self) -> dict:
        self._require()
        return self.db.one("SELECT * FROM accounts WHERE id=?", [self.account_id]) or {}

    def _require(self) -> None:
        if self.account_id is None:
            raise BrokerError("no account; run `stockagent init` first")

    # --------------------------------------------------------------------- cash
    def cash(self) -> float:
        self._require()
        v = self.db.scalar("SELECT COALESCE(SUM(amount),0) FROM cash_ledger WHERE account_id=?", [self.account_id])
        return round(float(v), 6)

    def settled_cash(self, as_of: date | None = None) -> float:
        self._require()
        as_of = as_of or self._today()
        v = self.db.scalar(
            "SELECT COALESCE(SUM(amount),0) FROM cash_ledger WHERE account_id=? AND settle_date<=?",
            [self.account_id, as_of.isoformat()],
        )
        return round(float(v), 6)

    def unsettled_credits(self, as_of: date | None = None) -> float:
        self._require()
        as_of = as_of or self._today()
        v = self.db.scalar(
            "SELECT COALESCE(SUM(amount),0) FROM cash_ledger WHERE account_id=? AND settle_date>? AND amount>0",
            [self.account_id, as_of.isoformat()],
        )
        return round(float(v), 6)

    def reserved_cash(self) -> float:
        self._require()
        v = self.db.scalar(
            f"SELECT COALESCE(SUM(reserved_cash),0) FROM orders WHERE account_id=? AND side='buy' "
            f"AND status IN {OPEN_STATUSES}",
            [self.account_id],
        )
        return round(float(v), 6)

    def buying_power(self, as_of: date | None = None) -> float:
        """Cash a new buy may use on `as_of` (default today). In a cash account,
        proceeds from a sale are not spendable until they settle unless
        allow_unsettled_buys is on. An order placed tonight for tomorrow's open
        may use proceeds that settle tomorrow."""
        bp = self.cash() - self.reserved_cash()
        if not self.cfg.risk.allow_unsettled_buys:
            bp -= self.unsettled_credits(as_of)
        return max(0.0, round(bp, 6))

    # ---------------------------------------------------------------- positions
    def positions(self) -> list[dict]:
        self._require()
        return self.db.all("SELECT * FROM positions WHERE account_id=? ORDER BY symbol", [self.account_id])

    def position(self, symbol: str) -> dict | None:
        self._require()
        return self.db.one("SELECT * FROM positions WHERE account_id=? AND symbol=?", [self.account_id, symbol])

    def trade(self, trade_id: int) -> dict | None:
        return self.db.one("SELECT * FROM trades WHERE id=?", [trade_id])

    def open_trades(self) -> list[dict]:
        self._require()
        return self.db.all("SELECT * FROM trades WHERE account_id=? AND status='open'", [self.account_id])

    def update_trade(self, trade_id: int, changes: dict) -> None:
        self.db.update("trades", trade_id, changes)

    def marks(self) -> dict[str, float]:
        return {r["symbol"]: r["price"] for r in self.db.all("SELECT symbol, price FROM marks")}

    def update_mark(self, quote: Quote) -> None:
        self.db.upsert(
            "marks",
            {
                "symbol": quote.symbol,
                "price": quote.last,
                "bid": quote.bid,
                "ask": quote.ask,
                "ts": (quote.ts or self.clock.now()).isoformat(),
            },
            ["symbol"],
        )

    def positions_value(self, marks: dict[str, float] | None = None) -> float:
        marks = marks if marks is not None else self.marks()
        total = 0.0
        for p in self.positions():
            total += p["qty"] * marks.get(p["symbol"], p["avg_price"])
        return round(total, 6)

    def equity(self, marks: dict[str, float] | None = None) -> float:
        return round(self.cash() + self.positions_value(marks), 6)

    # ------------------------------------------------------------------- orders
    def order(self, order_id: int) -> dict | None:
        return self.db.one("SELECT * FROM orders WHERE id=?", [order_id])

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        self._require()
        sql = f"SELECT * FROM orders WHERE account_id=? AND status IN {OPEN_STATUSES}"
        params: list = [self.account_id]
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        return self.db.all(sql + " ORDER BY id", params)

    def _committed_sell_qty(self, symbol: str, exclude_group: str | None = None) -> int:
        groups: dict[str, int] = {}
        for o in self.open_orders(symbol):
            if o["side"] != "sell":
                continue
            key = o["oco_group"] or f"#{o['id']}"
            if exclude_group and key == exclude_group:
                continue
            groups[key] = max(groups.get(key, 0), o["qty"] - o["filled_qty"])
        return sum(groups.values())

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        tif: str = "day",
        strategy: str | None = None,
        reason: str | None = None,
        ref_price: float | None = None,
        oco_group: str | None = None,
        trade_id: int | None = None,
        parent_id: int | None = None,
        active_from: datetime | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Validate and queue an order. Returns the order row; a rejected
        order is still stored, with status 'rejected' and the reason."""
        self._require()
        symbol = symbol.upper().strip()
        qty = int(qty)
        now = self._now()
        row = {
            "account_id": self.account_id,
            "created_at": now,
            "updated_at": now,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "type": type,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "tif": tif,
            "status": "open",
            "strategy": strategy,
            "reason": reason,
            "oco_group": oco_group,
            "trade_id": trade_id,
            "parent_id": parent_id,
            "active_from": (active_from or self.clock.now()).isoformat(),
            "meta": dumps(meta or {}),
        }
        problem = self._validate(row, ref_price)
        if problem:
            row["status"] = "rejected"
            row["meta"] = dumps({**(meta or {}), "reject_reason": problem})
            oid = self.db.insert("orders", row)
            self._journal("warn", "order_rejected", symbol, f"{side} {qty} {symbol} {type} rejected: {problem}", {"order_id": oid})
            return self.order(oid) or {}
        if side == "buy":
            row["reserved_cash"] = self._reserve_for(row, ref_price)
        oid = self.db.insert("orders", row)
        return self.order(oid) or {}

    def _estimate_buy_price(self, row: dict, ref_price: float | None) -> float | None:
        if row["type"] == "limit":
            return row["limit_price"]
        if row["type"] == "stop":
            return row["stop_price"]
        if ref_price:
            return ref_price
        mark = self.db.one("SELECT price, ask FROM marks WHERE symbol=?", [row["symbol"]])
        if mark:
            return mark["ask"] or mark["price"]
        return None

    def _reserve_for(self, row: dict, ref_price: float | None) -> float:
        px = self._estimate_buy_price(row, ref_price) or 0.0
        if row["type"] != "limit":
            px *= 1 + self.cfg.execution.market_slippage_pct + 0.02  # headroom for the spread
        notional = row["qty"] * px
        return round(notional + compute_fees(self.cfg.fees, "buy", row["qty"], px).total, 6)

    def _validate(self, row: dict, ref_price: float | None) -> str | None:
        if row["side"] not in ("buy", "sell"):
            return "side must be buy or sell (no short selling in a cash account)"
        if row["type"] not in ORDER_TYPES:
            return f"type must be one of {ORDER_TYPES}"
        if row["qty"] <= 0:
            return "qty must be a positive whole number of shares"
        if row["type"] == "limit" and not (row["limit_price"] and row["limit_price"] > 0):
            return "limit order needs a positive limit_price"
        if row["type"] == "stop" and not (row["stop_price"] and row["stop_price"] > 0):
            return "stop order needs a positive stop_price"
        if row["side"] == "sell":
            pos = self.position(row["symbol"])
            held = pos["qty"] if pos else 0
            committed = self._committed_sell_qty(row["symbol"], exclude_group=row["oco_group"])
            if row["qty"] + committed > held:
                return f"selling {row['qty']} but only {held - committed} of {held} shares are uncommitted"
            return None
        px = self._estimate_buy_price(row, ref_price)
        if not px:
            return "no price available to check buying power"
        need = self._reserve_for(row, ref_price)
        bp = self.buying_power(datetime.fromisoformat(row["active_from"]).astimezone(mcal.ET).date())
        if need > bp + 1e-9:
            return f"needs ${need:.2f}, buying power ${bp:.2f}"
        return None

    def cancel_order(self, order_id: int, reason: str = "cancelled", status: str = "cancelled") -> None:
        o = self.order(order_id)
        if not o or o["status"] not in OPEN_STATUSES:
            return
        meta = loads(o["meta"], {})
        meta["cancel_reason"] = reason
        self.db.update(
            "orders", order_id, {"status": status, "reserved_cash": 0, "updated_at": self._now(), "meta": dumps(meta)}
        )

    def cancel_all(self, symbol: str | None = None, side: str | None = None, reason: str = "cancelled") -> int:
        n = 0
        for o in self.open_orders(symbol):
            if side and o["side"] != side:
                continue
            self.cancel_order(o["id"], reason)
            n += 1
        return n

    def modify_order(self, order_id: int, limit_price: float | None = None, stop_price: float | None = None) -> None:
        o = self.order(order_id)
        if not o or o["status"] not in OPEN_STATUSES:
            raise BrokerError(f"order {order_id} is not open")
        changes: dict = {"updated_at": self._now()}
        if limit_price is not None:
            changes["limit_price"] = limit_price
        if stop_price is not None:
            changes["stop_price"] = stop_price
        self.db.update("orders", order_id, changes)

    def expire_day_orders(self) -> int:
        """Cancel day orders from earlier sessions, and every day order once
        the session has closed."""
        today = self._today()
        now = self.clock.now()
        _, close_dt = mcal.session_times(today) if mcal.is_trading_day(today) else (None, None)
        n = 0
        for o in self.open_orders():
            if o["tif"] != "day":
                continue
            active = datetime.fromisoformat(o["active_from"]).astimezone(mcal.ET)
            if active > now:
                continue
            if active.date() < today or (close_dt is not None and now >= close_dt):
                self.cancel_order(o["id"], "day order expired", status="expired")
                n += 1
        return n

    def place_bracket(self, trade_id: int, stop: float, target: float | None) -> None:
        """(Re)place the protective stop and profit target for a trade's whole position."""
        t = self.trade(trade_id)
        if not t:
            return
        pos = self.position(t["symbol"])
        group = f"T{trade_id}"
        for o in self.open_orders(t["symbol"]):
            if o["oco_group"] == group:
                self.cancel_order(o["id"], "bracket replaced")
        if not pos or pos["qty"] <= 0:
            return
        stop = round_to_tick(stop, "down")
        self.submit_order(
            t["symbol"], "sell", pos["qty"], "stop", stop_price=stop, tif="gtc",
            strategy=t["strategy"], reason="stop_loss", oco_group=group, trade_id=trade_id,
        )
        if target:
            self.submit_order(
                t["symbol"], "sell", pos["qty"], "limit", limit_price=round_to_tick(target, "up"), tif="gtc",
                strategy=t["strategy"], reason="profit_target", oco_group=group, trade_id=trade_id,
            )
        self.update_trade(trade_id, {"current_stop": stop, "target": target})

    def move_stop(self, trade_id: int, new_stop: float, reason: str) -> bool:
        t = self.trade(trade_id)
        if not t:
            return False
        new_stop = round_to_tick(new_stop, "down")
        if t["current_stop"] is not None and new_stop <= t["current_stop"]:
            return False  # stops only ever move up
        for o in self.open_orders(t["symbol"]):
            if o["oco_group"] == f"T{trade_id}" and o["type"] == "stop":
                self.modify_order(o["id"], stop_price=new_stop)
        self.update_trade(trade_id, {"current_stop": new_stop})
        self._journal("info", "stop_moved", t["symbol"], f"stop {t['current_stop']} -> {new_stop}: {reason}", {"trade_id": trade_id})
        return True

    def close_position(self, symbol: str, reason: str, quote: Quote | None = None, order_type: str = "market") -> dict | None:
        """Cancel the bracket and sell everything."""
        pos = self.position(symbol)
        if not pos:
            return None
        self.cancel_all(symbol, side="sell", reason=f"closing: {reason}")
        o = self.submit_order(symbol, "sell", pos["qty"], order_type, tif="day", reason=reason, trade_id=pos["trade_id"])
        if quote is not None and o.get("status") == "open":
            self.process_quote(quote)
        return o

    # ----------------------------------------------------------------- matching
    def _active(self, o: dict) -> bool:
        return datetime.fromisoformat(o["active_from"]) <= self.clock.now()

    def process_quote(self, q: Quote) -> list[int]:
        """Match open orders for one symbol against a live quote."""
        fills: list[int] = []
        bid, ask = q.bid_ask(self.cfg.execution)
        half = (ask - bid) / 2
        hi = max(q.high or q.last, q.last)
        lo = min(q.low or q.last, q.last)
        orders = sorted(
            (o for o in self.open_orders(q.symbol) if self._active(o)),
            key=lambda o: (_TYPE_PRIORITY.get(o["type"], 9), o["id"]),
        )
        for o in orders:
            o = self.order(o["id"])
            if not o or o["status"] not in OPEN_STATUSES:
                continue  # an OCO sibling filled earlier in this loop
            price = None
            t, side = o["type"], o["side"]
            # An order already checked against an earlier quote has been resting on
            # the book: it fills at its own price, not at a better quote that only
            # exists because we poll instead of seeing every print.
            resting = o["last_checked"] is not None
            if t in ("market", "moc"):
                price = buy_price(ask, self.cfg.execution) if side == "buy" else sell_price(bid, self.cfg.execution)
            elif t == "limit":
                lim = o["limit_price"]
                if side == "buy":
                    if ask <= lim:
                        price = lim if resting else ask
                    elif lo + half <= lim:
                        price = lim
                else:
                    if bid >= lim:
                        price = lim if resting else bid
                    elif hi - half >= lim:
                        price = lim
            elif t == "stop":
                stp = o["stop_price"]
                if side == "sell" and (lo <= stp or bid <= stp):
                    price = sell_price(min(stp, q.last) - half, self.cfg.execution)
                elif side == "buy" and (hi >= stp or ask >= stp):
                    price = buy_price(max(stp, q.last) + half, self.cfg.execution)
            if price is None:
                continue
            qty = o["qty"] - o["filled_qty"]
            fid = self._fill(o, qty, price, q.mid())
            if fid:
                fills.append(fid)
        self.db.execute(
            f"UPDATE orders SET last_checked=? WHERE account_id=? AND symbol=? AND status IN {OPEN_STATUSES}",
            [self._now(), self.account_id, q.symbol],
        )
        return fills

    def process_bar(self, symbol: str, bar: Bar, phase: str, avg_dollar_volume: float | None = None) -> list[int]:
        """Match open orders against a daily bar. Phases run in order each day:
        'open' (gaps), 'intraday' (the high/low range), 'close' (MOC orders)."""
        fills: list[int] = []
        half = estimate_spread(bar.open if phase == "open" else bar.close, avg_dollar_volume, self.cfg.execution) / 2
        orders = sorted(
            (o for o in self.open_orders(symbol) if self._active(o)),
            key=lambda o: (_TYPE_PRIORITY.get(o["type"], 9), o["id"]),
        )
        ex = self.cfg.execution
        for o in orders:
            o = self.order(o["id"])
            if not o or o["status"] not in OPEN_STATUSES:
                continue
            t, side = o["type"], o["side"]
            price = None
            mid = None
            if phase == "open":
                mid = bar.open
                ask, bid = bar.open + half, max(bar.open - half, tick_size(bar.open))
                if t == "market":
                    price = buy_price(ask, ex) if side == "buy" else sell_price(bid, ex)
                elif t == "limit":
                    if side == "buy" and ask <= o["limit_price"]:
                        price = round_to_tick(ask, "up")
                    elif side == "sell" and bid >= o["limit_price"]:
                        price = round_to_tick(bid, "down")
                elif t == "stop":
                    if side == "sell" and bar.open <= o["stop_price"]:
                        price = sell_price(bid, ex)
                    elif side == "buy" and bar.open >= o["stop_price"]:
                        price = buy_price(ask, ex)
            elif phase == "intraday":
                if t == "limit":
                    lim = o["limit_price"]
                    mid = lim
                    if side == "buy" and bar.low <= lim - half:
                        price = lim
                    elif side == "sell" and bar.high >= lim + half:
                        price = lim
                elif t == "stop":
                    stp = o["stop_price"]
                    mid = stp
                    if side == "sell" and bar.low <= stp:
                        price = sell_price(stp - half, ex)
                    elif side == "buy" and bar.high >= stp:
                        price = buy_price(stp + half, ex)
                elif t == "market":
                    mid = bar.open
                    price = buy_price(bar.open + half, ex) if side == "buy" else sell_price(bar.open - half, ex)
            elif phase == "close" and t == "moc":
                mid = bar.close
                price = round_to_tick(bar.close + half, "up") if side == "buy" else round_to_tick(
                    max(bar.close - half, tick_size(bar.close)), "down"
                )
            if price is None:
                continue
            qty = o["qty"] - o["filled_qty"]
            cap = int(bar.volume * ex.max_pct_of_volume) if bar.volume else qty
            qty = min(qty, cap)
            if qty <= 0:
                continue
            fid = self._fill(o, qty, price, mid if mid is not None else price)
            if fid:
                fills.append(fid)
        return fills

    # -------------------------------------------------------------------- fills
    def _fill(self, o: dict, qty: int, price: float, mid: float) -> int | None:
        price = round(float(price), 4)
        today = self._today()
        settle = mcal.settlement_date(today if mcal.is_trading_day(today) else mcal.prev_trading_day(today))
        fees = compute_fees(self.cfg.fees, o["side"], qty, price)
        spread_cost = round(abs(price - mid) * qty, 6)
        symbol = o["symbol"]
        now = self._now()
        meta = loads(o["meta"], {})
        with self.db.transaction() as db:
            pos = self.position(symbol)
            trade_id = o["trade_id"] or (pos["trade_id"] if pos else None)
            if o["side"] == "buy":
                cost = qty * price + fees.total
                sellable_after = None
                if self.cfg.risk.allow_unsettled_buys:
                    settled_room = self.cash() - self.unsettled_credits() - (self.reserved_cash() - o["reserved_cash"])
                    if cost > settled_room + 1e-9:
                        latest = db.scalar(
                            "SELECT MAX(settle_date) FROM cash_ledger WHERE account_id=? AND settle_date>? AND amount>0",
                            [self.account_id, today.isoformat()],
                        )
                        sellable_after = latest
                if trade_id is None:
                    trade_id = db.insert(
                        "trades",
                        {
                            "account_id": self.account_id,
                            "symbol": symbol,
                            "strategy": o["strategy"],
                            "status": "open",
                            "opened_at": now,
                            "max_hold_days": meta.get("max_hold_days"),
                            "atr": meta.get("atr"),
                            "signal_id": meta.get("signal_id"),
                            "tip_ids": dumps(meta.get("tip_ids", [])),
                            "high_water": price,
                            "meta": dumps({k: meta[k] for k in ("stop_distance", "reward_multiple") if k in meta}),
                        },
                    )
                db.insert(
                    "cash_ledger",
                    {
                        "account_id": self.account_id, "ts": now, "trade_date": today.isoformat(),
                        "settle_date": settle.isoformat(), "amount": -round(cost, 6), "kind": "buy", "ref_id": o["id"],
                    },
                )
                if pos:
                    new_qty = pos["qty"] + qty
                    db.update(
                        "positions",
                        pos["id"],
                        {
                            "qty": new_qty,
                            "avg_price": (pos["avg_price"] * pos["qty"] + price * qty) / new_qty,
                            "cost_basis": pos["cost_basis"] + cost,
                            "sellable_after": max(filter(None, [pos["sellable_after"], sellable_after]), default=None),
                        },
                    )
                else:
                    db.insert(
                        "positions",
                        {
                            "account_id": self.account_id, "symbol": symbol, "qty": qty, "avg_price": price,
                            "cost_basis": cost, "opened_at": now, "trade_id": trade_id, "sellable_after": sellable_after,
                        },
                    )
                db.execute(
                    "UPDATE trades SET entry_qty=entry_qty+?, entry_value=entry_value+?, fees=fees+?, spread_cost=spread_cost+? WHERE id=?",
                    [qty, qty * price, fees.total, spread_cost, trade_id],
                )
            else:
                if not pos or pos["qty"] < qty:
                    self.cancel_order(o["id"], "position smaller than order")
                    return None
                if pos["sellable_after"] and today.isoformat() < pos["sellable_after"]:
                    db.execute("UPDATE accounts SET gfv_count=gfv_count+1 WHERE id=?", [self.account_id])
                    self._journal(
                        "warn", "good_faith_violation", symbol,
                        f"sold {symbol} bought with unsettled funds before {pos['sellable_after']}", {"order_id": o["id"]},
                    )
                db.insert(
                    "cash_ledger",
                    {
                        "account_id": self.account_id, "ts": now, "trade_date": today.isoformat(),
                        "settle_date": settle.isoformat(), "amount": round(qty * price - fees.total, 6), "kind": "sell",
                        "ref_id": o["id"],
                    },
                )
                remaining = pos["qty"] - qty
                db.execute(
                    "UPDATE trades SET exit_qty=exit_qty+?, exit_value=exit_value+?, fees=fees+?, spread_cost=spread_cost+? WHERE id=?",
                    [qty, qty * price, fees.total, spread_cost, trade_id],
                )
                if remaining == 0:
                    db.execute("DELETE FROM positions WHERE id=?", [pos["id"]])
                else:
                    db.update(
                        "positions", pos["id"],
                        {"qty": remaining, "cost_basis": pos["cost_basis"] * remaining / pos["qty"]},
                    )
            fill_id = db.insert(
                "fills",
                {
                    "order_id": o["id"], "account_id": self.account_id, "ts": now, "symbol": symbol, "side": o["side"],
                    "qty": qty, "price": price, "mid_price": mid, "commission": fees.commission, "sec_fee": fees.sec_fee,
                    "taf_fee": fees.taf_fee, "cat_fee": fees.cat_fee, "total_fees": fees.total,
                    "spread_cost": spread_cost, "settle_date": settle.isoformat(), "trade_id": trade_id,
                },
            )
            filled = o["filled_qty"] + qty
            avg = ((o["avg_fill_price"] or 0) * o["filled_qty"] + price * qty) / filled
            status = "filled" if filled >= o["qty"] else "partial"
            reserved = 0 if status == "filled" else o["reserved_cash"] * (o["qty"] - filled) / o["qty"]
            db.update(
                "orders", o["id"],
                {"filled_qty": filled, "avg_fill_price": avg, "status": status, "updated_at": now,
                 "reserved_cash": reserved, "trade_id": trade_id},
            )
            if status == "filled" and o["oco_group"]:
                for sib in self.open_orders(symbol):
                    if sib["oco_group"] == o["oco_group"] and sib["id"] != o["id"]:
                        self.cancel_order(sib["id"], f"OCO sibling {o['id']} filled")

        self._journal(
            "info", "fill", symbol,
            f"{o['side'].upper()} {qty} {symbol} @ {price:.4f} ({o['type']}{', ' + o['reason'] if o['reason'] else ''}) "
            f"fees ${fees.total:.2f}",
            {"order_id": o["id"], "fill_id": fill_id, "trade_id": trade_id},
        )
        if o["side"] == "buy":
            self._after_entry_fill(trade_id, meta)
        else:
            self._after_exit_fill(trade_id, symbol, o)
        return fill_id

    def _after_entry_fill(self, trade_id: int, meta: dict) -> None:
        t = self.trade(trade_id)
        if not t or t["entry_qty"] <= 0:
            return
        entry = t["entry_value"] / t["entry_qty"]
        dist = meta.get("stop_distance")
        if not dist:
            return
        stop = max(entry - dist, tick_size(entry))
        rr = meta.get("reward_multiple")
        target = entry + rr * dist if rr else None
        changes = {"initial_risk": round((entry - stop) * t["entry_qty"], 6)}
        if t["initial_stop"] is None:
            changes["initial_stop"] = round_to_tick(stop, "down")
        self.update_trade(trade_id, changes)
        self.place_bracket(trade_id, stop, target)

    def _after_exit_fill(self, trade_id: int, symbol: str, o: dict) -> None:
        if self.position(symbol):
            return
        t = self.trade(trade_id)
        if not t or t["status"] != "open":
            return
        gross = t["exit_value"] - t["entry_value"]
        net = gross - t["fees"]
        risk = t["initial_risk"] or 0
        self.update_trade(
            trade_id,
            {
                "status": "closed",
                "closed_at": self._now(),
                "gross_pnl": round(gross, 6),
                "net_pnl": round(net, 6),
                "return_pct": round(net / t["entry_value"], 6) if t["entry_value"] else None,
                "r_multiple": round(net / risk, 4) if risk > 0 else None,
                "exit_reason": o["reason"] or o["type"],
            },
        )
        self.cancel_all(symbol, reason="position closed")
        self._journal(
            "info", "trade_closed", symbol,
            f"closed {symbol} ({t['strategy']}): net ${net:+.2f} after ${t['fees']:.2f} fees, exit: {o['reason'] or o['type']}",
            {"trade_id": trade_id},
        )

    # ------------------------------------------------------------- bookkeeping
    def record_equity(self, kind: str, target_equity: float, marks: dict[str, float] | None = None) -> float:
        self._require()
        marks = marks if marks is not None else self.marks()
        eq = self.equity(marks)
        today = self._today()
        self.db.insert(
            "equity",
            {
                "account_id": self.account_id, "ts": self._now(), "day": today.isoformat(), "kind": kind,
                "equity": eq, "cash": self.cash(), "settled_cash": self.settled_cash(),
                "positions_value": self.positions_value(marks), "target_equity": target_equity,
            },
        )
        acct = self.account()
        if eq > acct["peak_equity"]:
            self.db.update("accounts", self.account_id, {"peak_equity": eq})
        return eq

    def _journal(self, level: str, event: str, symbol: str | None, message: str, data: dict | None = None) -> None:
        self.db.insert(
            "journal",
            {"ts": self._now(), "level": level, "event": event, "symbol": symbol, "message": message, "data": dumps(data or {})},
        )


def shares_for(cash: float, price: float) -> int:
    return int(math.floor(cash / price)) if price > 0 else 0
