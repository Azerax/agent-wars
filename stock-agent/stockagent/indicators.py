"""Technical indicators over a daily OHLCV DataFrame (oldest row first)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0)


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def bandwidth(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    lo, mid, hi = bollinger(s, n, k)
    return (hi - lo) / mid


def relative_volume(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Today's volume over the average of the previous n days."""
    return df["volume"] / df["volume"].shift(1).rolling(n, min_periods=max(5, n // 2)).mean()


def highest(s: pd.Series, n: int, shift: int = 1) -> pd.Series:
    return s.shift(shift).rolling(n, min_periods=n).max()


def close_location(df: pd.DataFrame) -> pd.Series:
    """Where the close sits in the day's range: 0 = at the low, 1 = at the high."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return ((df["close"] - df["low"]) / rng).fillna(0.5)


def pct_change(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change()


def gap_pct(df: pd.DataFrame) -> pd.Series:
    return df["open"] / df["close"].shift(1) - 1


def percentile_rank(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=max(20, window // 2)).apply(
        lambda x: (x[:-1] < x[-1]).mean() if len(x) > 1 else np.nan, raw=True
    )
