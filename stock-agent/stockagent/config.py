"""Configuration.

Every number that changes how the agent trades lives here, with the reason it
has the default it has. Override any of it from a TOML file (see
config.example.toml) or from environment variables for the secrets.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class FeeConfig:
    """Transaction costs charged by the simulated broker.

    Defaults model a US zero-commission broker that passes regulatory fees
    through, which is what a $100 account would realistically use.
    """

    preset: str = "zero_commission"
    commission_per_trade: float = 0.0
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    # Cap on commission as a fraction of trade value (IBKR-style). 0 = no cap.
    commission_max_pct: float = 0.0
    # SEC Section 31 fee, sells only. $20.60 per $1M sold from 2026-04-04 (FY2026).
    sec_fee_per_million: float = 20.60
    # FINRA Trading Activity Fee, sells only. 2026 schedule: $0.000195/share, max $9.79.
    finra_taf_per_share: float = 0.000195
    finra_taf_max: float = 9.79
    # Consolidated Audit Trail fee some brokers pass through, per share, both sides.
    cat_fee_per_share: float = 0.0


@dataclass
class ExecutionConfig:
    """How simulated fills are priced. The spread is the biggest hidden cost
    on low-priced stocks, much bigger than the explicit fees."""

    # Full quoted spread = max(tick, price * (base + coef / sqrt(avg $ volume in $M)))
    base_spread_pct: float = 0.001
    liquidity_spread_coef: float = 0.01
    # Extra adverse move on market and triggered stop orders.
    market_slippage_pct: float = 0.002
    # Never fill more than this fraction of a bar's volume.
    max_pct_of_volume: float = 0.02
    # Entry orders are marketable limits: never pay more than ref * (1 + this).
    max_chase_pct: float = 0.015


@dataclass
class UniverseConfig:
    """What counts as a "low price stock" the agent may trade."""

    min_price: float = 0.50
    max_price: float = 5.00
    # Below roughly $1M a day the spread eats any edge and fills are fiction.
    min_avg_dollar_volume: float = 1_000_000.0
    min_volume: int = 100_000
    exchanges: list[str] = field(default_factory=lambda: ["nasdaq", "nyse", "amex"])
    # Warrants, rights, units and preferreds behave nothing like the common stock.
    exclude_symbol_patterns: list[str] = field(
        default_factory=lambda: [r"\^", r"/", r"\.", r"-", r"^[A-Z]{4}[WRU]$", r"^[A-Z]{3,4}WS$"]
    )


@dataclass
class RiskConfig:
    starting_cash: float = 100.0
    max_positions: int = 2
    # Fraction of equity lost if the stop is hit. Aggressive on purpose: the
    # goal is aggressive. It does NOT grow when the agent falls behind target.
    risk_per_trade_pct: float = 0.08
    max_position_pct: float = 0.65
    min_position_value: float = 10.0
    # Stop opening positions for the day after losing this fraction of the
    # day's starting equity.
    daily_loss_limit_pct: float = 0.15
    # Stop opening positions entirely once equity falls this far below its peak.
    max_drawdown_halt_pct: float = 0.50
    # Skip a trade when round-trip costs (fees + spread) exceed this fraction
    # of the distance to its profit target.
    max_cost_to_reward: float = 0.25
    # Cash account: only spend settled cash unless this is on. On, the broker
    # lets you buy with unsettled proceeds and records good-faith violations
    # when such a position is sold before the funds settle.
    allow_unsettled_buys: bool = False
    max_hold_days: int = 4
    no_entry_first_minutes: int = 5
    no_entry_last_minutes: int = 15
    time_stop_minutes_before_close: int = 10
    # Move the stop to break-even (plus fees) at +breakeven_at_r, trail after +trail_at_r.
    breakeven_at_r: float = 1.0
    trail_at_r: float = 2.0
    trail_atr_mult: float = 1.0


@dataclass
class TargetConfig:
    """The goal the agent measures itself against: multiple x every N trading days."""

    multiple: float = 2.0
    period_trading_days: int = 4


@dataclass
class AgentConfig:
    poll_seconds: int = 60
    scout_interval_minutes: int = 30
    watchlist_size: int = 25
    strategies: list[str] = field(
        default_factory=lambda: [
            "momentum_breakout",
            "squeeze_breakout",
            "gap_and_go",
            "oversold_bounce",
            "catalyst_momentum",
            "social_momentum",
        ]
    )
    # Adapt strategy weights from the agent's own closed trades.
    adaptive: bool = True
    # Quarantine a strategy once it has this many trades with mean net R below the floor.
    quarantine_min_trades: int = 15
    quarantine_r_floor: float = -0.3


@dataclass
class LLMConfig:
    """Optional: Claude reads headlines and filings and grades the catalyst.
    Off unless an Anthropic credential is available; the lexicon grader is
    always used as the fallback."""

    enabled: str = "auto"  # auto | on | off
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_calls_per_day: int = 40


@dataclass
class DataConfig:
    db_path: str = "data/stockagent.db"
    cache_path: str = "data/http_cache.db"
    # Identify the client honestly. A site that refuses automated clients is
    # treated as unavailable, not worked around.
    user_agent: str = "stockagent/0.1 (paper-trading research; read-only)"
    # SEC asks automated clients to identify themselves with a contact address.
    # Set STOCKAGENT_SEC_USER_AGENT="Your Name you@example.com"; SEC sources are
    # skipped when it is empty.
    sec_user_agent: str = ""
    request_timeout: float = 15.0
    min_request_interval: float = 0.35
    # Alpha Vantage. The key is read from the ALPHA or ALPHAVANTAGE_API_KEY
    # environment variable, or from a `.alpha` file (ALPHA=yourkey) in the
    # working directory or the project folder. Keep it out of config.toml.
    # Where prices, bars, the stock universe and news come from:
    #   "alpaca" - only Alpaca (licensed; needs a free account's keys)
    #   "legacy" - Yahoo and Nasdaq web endpoints (not licensed for automated use)
    #   "auto"   - Alpaca when its keys are found, otherwise legacy
    provider: str = "auto"
    # Filled from ALPACA_KEY / ALPACA_SECRET or a .alpaca file; keep them out of config.toml.
    alpaca_key: str = ""
    alpaca_secret: str = ""
    alpaca_key_file: str = ".alpaca"
    alphavantage_api_key: str = ""
    alphavantage_key_file: str = ".alpha"
    alphavantage_daily_limit: int = 25  # free key: 25 requests/day
    alphavantage_per_minute: int = 5
    sources: list[str] = field(
        default_factory=lambda: [
            "yahoo_screeners",
            "nasdaq_movers",
            "alphavantage",
            "stocktwits",
            "reddit",
            "sec_filings",
            "news",
        ]
    )
    reddit_subs: list[str] = field(
        default_factory=lambda: ["pennystocks", "smallstreetbets", "RobinHoodPennyStocks"]
    )


@dataclass
class Config:
    fees: FeeConfig = field(default_factory=FeeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    data: DataConfig = field(default_factory=DataConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FEE_PRESETS: dict[str, dict[str, float]] = {
    # Robinhood / Webull / Schwab-style: no commission, regulatory fees passed through.
    "zero_commission": {
        "commission_per_trade": 0.0,
        "commission_per_share": 0.0,
        "commission_min": 0.0,
        "commission_max_pct": 0.0,
    },
    # Interactive Brokers Pro fixed: $0.005/share, $1 minimum, capped at 1% of value.
    "ibkr_pro_fixed": {
        "commission_per_trade": 0.0,
        "commission_per_share": 0.005,
        "commission_min": 1.0,
        "commission_max_pct": 0.01,
    },
    # A legacy flat-fee broker, for stress-testing how fees kill small accounts.
    "flat_4_95": {
        "commission_per_trade": 4.95,
        "commission_per_share": 0.0,
        "commission_min": 0.0,
        "commission_max_pct": 0.0,
    },
}


def _merge(obj: Any, data: dict[str, Any]) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {type(obj).__name__}.{key}")
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(obj, key, value)


KEY_NAMES = ("ALPHA", "ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY")
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _read_text_any(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            if "\x00" not in text:
                return text
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_key_file(text: str) -> str | None:
    """Accepts `ALPHA=key`, `$env:ALPHA = "key"`, `set ALPHA=key`,
    `export ALPHA=key`, or just the key on its own line."""
    bare = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            name = re.sub(r"^(\$env:|export\s+|set\s+)", "", name.strip(), flags=re.I).strip()
            if name.upper() in KEY_NAMES:
                value = value.strip().strip("'\"").strip()
                if value:
                    return value
        elif bare is None and re.fullmatch(r"[A-Za-z0-9]{8,64}", line):
            bare = line
    return bare


def find_alphavantage_key(cfg_key: str = "", key_file: str = ".alpha") -> tuple[str, str]:
    """Return (key, where it came from). Never log the key itself."""
    for name in KEY_NAMES:
        if os.environ.get(name, "").strip():
            return os.environ[name].strip(), f"environment variable {name}"
    candidates = []
    for base in (Path.cwd(), PROJECT_DIR):
        for name in (key_file, key_file + ".txt"):
            p = base / name
            if p not in candidates:
                candidates.append(p)
    for p in candidates:
        if p.is_file():
            key = parse_key_file(_read_text_any(p))
            if key:
                return key, f"file {p}"
    if cfg_key:
        return cfg_key, "config file"
    return "", "not found"


ALPACA_KEY_NAMES = ("ALPACA_KEY", "ALPACA_API_KEY", "ALPACA_KEY_ID", "APCA_API_KEY_ID")
ALPACA_SECRET_NAMES = ("ALPACA_SECRET", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET", "APCA_API_SECRET_KEY")


def parse_alpaca_file(text: str) -> tuple[str, str]:
    """Accepts NAME=value or NAME: value lines (any name containing KEY/ID for
    the key and SECRET for the secret), or the two values bare, key first."""
    key = secret = ""
    bare = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(?:\$env:|export\s+|set\s+)?([A-Za-z_ ][A-Za-z0-9_ ]*?)\s*[=:]\s*(.+)$", line)
        if m:
            name = m.group(1).strip().upper().replace(" ", "_")
            value = m.group(2).strip().strip("'\"").strip()
            if "SECRET" in name:
                secret = secret or value
            elif "KEY" in name or name.endswith("ID"):
                key = key or value
        elif re.fullmatch(r"[A-Za-z0-9]{10,80}", line):
            bare.append(line)
    if not key and bare:
        key = bare.pop(0)
    if not secret and bare:
        secret = bare.pop(0)
    return key, secret


def find_alpaca_keys(key_file: str = ".alpaca") -> tuple[str, str, str]:
    """Return (key, secret, where they came from). Never log the values."""
    env_key = next((os.environ[n].strip() for n in ALPACA_KEY_NAMES if os.environ.get(n, "").strip()), "")
    env_secret = next((os.environ[n].strip() for n in ALPACA_SECRET_NAMES if os.environ.get(n, "").strip()), "")
    if env_key and env_secret:
        return env_key, env_secret, "environment variables"
    for base in (Path.cwd(), PROJECT_DIR):
        for name in (key_file, key_file + ".txt"):
            p = base / name
            if p.is_file():
                key, secret = parse_alpaca_file(_read_text_any(p))
                if key and secret:
                    return key, secret, f"file {p}"
                return "", "", f"file {p} (could not find both a key and a secret in it)"
    return "", "", "not found"


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg = Config()
    if path is None:
        env_path = os.environ.get("STOCKAGENT_CONFIG")
        if env_path:
            path = env_path
        elif Path("config.toml").exists():
            path = "config.toml"
    explicit_fee_keys: set[str] = set()
    if path is not None:
        # Windows editors often save UTF-16 or UTF-8 with a BOM; tomllib wants plain UTF-8.
        raw = tomllib.loads(_read_text_any(Path(path)))
        explicit_fee_keys = set(raw.get("fees", {}).keys())
        _merge(cfg, raw)
    preset = FEE_PRESETS.get(cfg.fees.preset)
    if preset is None:
        raise ValueError(f"unknown fee preset {cfg.fees.preset!r}; choose from {sorted(FEE_PRESETS)}")
    for key, value in preset.items():
        if key not in explicit_fee_keys:
            setattr(cfg.fees, key, value)
    sec_ua = os.environ.get("STOCKAGENT_SEC_USER_AGENT")
    if sec_ua:
        cfg.data.sec_user_agent = sec_ua
    key, _where = find_alphavantage_key(cfg.data.alphavantage_api_key, cfg.data.alphavantage_key_file)
    cfg.data.alphavantage_api_key = key
    if not (cfg.data.alpaca_key and cfg.data.alpaca_secret):
        cfg.data.alpaca_key, cfg.data.alpaca_secret, _where = find_alpaca_keys(cfg.data.alpaca_key_file)
    if cfg.data.provider not in ("auto", "alpaca", "legacy"):
        raise ValueError(f"data.provider must be auto, alpaca or legacy, not {cfg.data.provider!r}")
    db = os.environ.get("STOCKAGENT_DB")
    if db:
        cfg.data.db_path = db
    return cfg
