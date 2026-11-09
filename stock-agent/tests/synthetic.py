"""Synthetic price series for tests only. Never used for reported results."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from stockagent import clock as mcal


def make_bars(start: date, end: date, seed: int, price: float = 2.0, vol: float = 0.05,
              base_volume: float = 2_000_000, jumps: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = mcal.trading_days_in_range(start, end)
    n = len(days)
    rets = rng.normal(0.0005, vol, n)
    volume = base_volume * rng.lognormal(0, 0.3, n)
    for k in rng.choice(np.arange(60, n - 5), size=min(jumps, max(n - 65, 0)), replace=False):
        rets[k] += rng.uniform(0.15, 0.4)
        volume[k] *= rng.uniform(4, 9)
        rets[k + 1] += rng.uniform(0.0, 0.2)
        volume[k + 1] *= 3
    close = price * np.exp(np.cumsum(rets))
    open_ = np.r_[price, close[:-1]] * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=mcal.ET) for d in days])
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
