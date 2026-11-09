"""SEC EDGAR: the latest filings as they are accepted.

Two things matter for low-priced stocks. 8-K items announce catalysts
(contracts, acquisitions) and red flags (delisting notices, bankruptcy).
Prospectus filings (424B*, S-1, S-3) announce share offerings, and an
offering is the most common reason a penny stock's rally dies.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET_XML
from datetime import datetime
from typing import Any

from .http import DataError, HttpClient

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ATOM = "{http://www.w3.org/2005/Atom}"

DILUTION_FORMS = ("424B1", "424B2", "424B3", "424B4", "424B5", "S-1", "S-3", "F-1", "F-3", "S-1/A", "S-3/A")

_TITLE_RE = re.compile(r"^(?P<form>[^ ]+(?: [^ ]+)?) - (?P<company>.*) \((?P<cik>\d{4,10})\) \((?P<role>[^)]+)\)")
_ITEM_RE = re.compile(r"Item (\d+\.\d+)")


def parse_current_feed(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET_XML.fromstring(xml_text)
    except ET_XML.ParseError as exc:
        raise DataError(f"sec feed: bad XML ({exc})") from exc
    out = []
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        m = _TITLE_RE.match(title)
        if not m:
            continue
        link_el = entry.find(f"{ATOM}link")
        summary = html.unescape(entry.findtext(f"{ATOM}summary") or "")
        updated = entry.findtext(f"{ATOM}updated")
        out.append(
            {
                "form": m.group("form").strip(),
                "company": m.group("company").strip(),
                "cik": int(m.group("cik")),
                "role": m.group("role"),
                "items": sorted(set(_ITEM_RE.findall(summary))),
                "summary": re.sub(r"<[^>]+>", " ", summary).strip(),
                "url": link_el.get("href") if link_el is not None else None,
                "updated": datetime.fromisoformat(updated) if updated else None,
            }
        )
    return out


def parse_ticker_map(payload: dict[str, Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in payload.values():
        try:
            cik = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(cik, str(row.get("ticker", "")).upper())
    return out


class SEC:
    def __init__(self, http: HttpClient, user_agent: str):
        self.http = http
        self.user_agent = user_agent
        self._tickers: dict[int, str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.user_agent)

    def _headers(self) -> dict[str, str]:
        if not self.enabled:
            raise DataError("SEC source disabled: set STOCKAGENT_SEC_USER_AGENT to 'Name email@example.com'")
        return {"User-Agent": self.user_agent, "Accept": "application/atom+xml, application/json, */*"}

    def ticker_map(self) -> dict[int, str]:
        if self._tickers is None:
            payload = self.http.get_json(TICKERS_URL, headers=self._headers(), ttl=86400)
            self._tickers = parse_ticker_map(payload)
        return self._tickers

    def current(self, form: str = "8-K", count: int = 100) -> list[dict[str, Any]]:
        text = self.http.get_text(
            FEED_URL,
            params={"action": "getcurrent", "type": form, "company": "", "dateb": "", "owner": "include",
                    "start": 0, "count": count, "output": "atom"},
            headers=self._headers(),
            ttl=300,
        )
        filings = parse_current_feed(text)
        tickers = self.ticker_map()
        for f in filings:
            f["symbol"] = tickers.get(f["cik"])
        return filings
