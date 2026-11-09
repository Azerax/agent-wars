"""Stooq daily bars, the fallback when Yahoo's chart endpoint fails."""
from __future__ import annotations

import io

import pandas as pd

from .http import DataError, HttpClient

URL = "https://stooq.com/q/d/l/"


def parse_csv(text: str) -> pd.DataFrame:
    if not text or not text.lstrip().lower().startswith("date"):
        raise DataError(f"stooq: unexpected response {text[:80]!r}")
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
    df.index = df.index.tz_localize("America/New_York")
    return df.sort_index()


class Stooq:
    def __init__(self, http: HttpClient):
        self.http = http

    def daily(self, symbol: str, ttl: float = 3600) -> pd.DataFrame:
        text = self.http.get_text(URL, params={"s": f"{symbol.lower()}.us", "i": "d"}, ttl=ttl)
        return parse_csv(text)
