"""SQLite storage. Everything the agent sees, decides and does is written here,
so any result can be traced back to the tip, signal, order and fill behind it."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    start_date TEXT NOT NULL,
    peak_equity REAL NOT NULL,
    gfv_count INTEGER NOT NULL DEFAULT 0
);
-- Every cash movement, with the date it settles. Balances are sums over this.
CREATE TABLE IF NOT EXISTS cash_ledger (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    settle_date TEXT NOT NULL,
    amount REAL NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    type TEXT NOT NULL,
    limit_price REAL,
    stop_price REAL,
    tif TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    parent_id INTEGER,
    oco_group TEXT,
    trade_id INTEGER,
    strategy TEXT,
    reason TEXT,
    reserved_cash REAL NOT NULL DEFAULT 0,
    active_from TEXT,
    last_checked TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(account_id, status);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    mid_price REAL,
    commission REAL NOT NULL,
    sec_fee REAL NOT NULL,
    taf_fee REAL NOT NULL,
    cat_fee REAL NOT NULL,
    total_fees REAL NOT NULL,
    spread_cost REAL NOT NULL DEFAULT 0,
    settle_date TEXT NOT NULL,
    trade_id INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    cost_basis REAL NOT NULL,
    opened_at TEXT NOT NULL,
    trade_id INTEGER NOT NULL,
    sellable_after TEXT,
    UNIQUE(account_id, symbol)
);
-- One row per round trip. Opened on the first buy fill, closed when flat.
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    entry_qty INTEGER NOT NULL DEFAULT 0,
    entry_value REAL NOT NULL DEFAULT 0,
    exit_qty INTEGER NOT NULL DEFAULT 0,
    exit_value REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    spread_cost REAL NOT NULL DEFAULT 0,
    initial_stop REAL,
    current_stop REAL,
    target REAL,
    initial_risk REAL,
    atr REAL,
    max_hold_days INTEGER,
    high_water REAL,
    gross_pnl REAL,
    net_pnl REAL,
    return_pct REAL,
    r_multiple REAL,
    exit_reason TEXT,
    signal_id INTEGER,
    tip_ids TEXT,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    settled_cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    target_equity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_day ON equity(account_id, day, kind);
-- Four-trading-day cycles measured against the doubling target.
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    start_equity REAL NOT NULL,
    target_equity REAL NOT NULL,
    end_equity REAL,
    multiple REAL,
    hit INTEGER,
    UNIQUE(account_id, number)
);
CREATE TABLE IF NOT EXISTS tips (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT,
    url TEXT,
    raw_score REAL NOT NULL,
    price_at_tip REAL,
    eligible INTEGER NOT NULL DEFAULT 1,
    reject_reason TEXT,
    payload TEXT,
    fwd_1d REAL,
    fwd_4d REAL,
    evaluated_1d INTEGER NOT NULL DEFAULT 0,
    evaluated_4d INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tips_symbol_day ON tips(symbol, day);
CREATE INDEX IF NOT EXISTS idx_tips_source ON tips(source);
CREATE TABLE IF NOT EXISTS watchlist (
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    score REAL NOT NULL,
    sources TEXT,
    catalyst_score REAL,
    dilution_risk INTEGER,
    data TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(day, symbol)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    score REAL NOT NULL,
    final_score REAL,
    ref_price REAL,
    stop_distance REAL,
    reward_multiple REAL,
    decision TEXT,
    decision_reason TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS catalysts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    grader TEXT NOT NULL,
    score REAL NOT NULL,
    dilution_risk INTEGER NOT NULL,
    tags TEXT,
    summary TEXT,
    input_hash TEXT
);
CREATE TABLE IF NOT EXISTS marks (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    bid REAL,
    ask REAL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    symbol TEXT,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
"""


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row.values())
        return int(cur.lastrowid)

    def upsert(self, table: str, row: dict[str, Any], keys: list[str]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c not in keys)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({', '.join(keys)}) DO "
        sql += f"UPDATE SET {updates}" if updates else "NOTHING"
        self.execute(sql, row.values())

    def update(self, table: str, row_id: int, changes: dict[str, Any]) -> None:
        sets = ", ".join(f"{c}=?" for c in changes)
        self.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*changes.values(), row_id])

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.execute(sql, params).fetchone()
        return row[0] if row else None

    def transaction(self) -> "_Tx":
        return _Tx(self)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", [key])
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.upsert("meta", {"key": key, "value": value}, ["key"])

    def close(self) -> None:
        self.conn.close()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db.conn.execute("COMMIT")
            else:
                self.db.conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    return json.loads(text)
