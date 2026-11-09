"""Headline feeds (RSS). Combined with Yahoo's search API in the provider."""
from __future__ import annotations

import xml.etree.ElementTree as ET_XML
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

from .http import DataError, HttpClient

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"
GOOGLE_RSS = "https://news.google.com/rss/search"


def parse_rss(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET_XML.fromstring(xml_text)
    except ET_XML.ParseError as exc:
        raise DataError(f"rss: bad XML ({exc})") from exc
    out = []
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        source_el = item.find("source")
        out.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "url": item.findtext("link"),
                "publisher": source_el.text if source_el is not None else None,
                "published": published,
                "tickers": [],
            }
        )
    return out


class News:
    def __init__(self, http: HttpClient):
        self.http = http

    def yahoo_rss(self, symbol: str) -> list[dict[str, Any]]:
        text = self.http.get_text(YAHOO_RSS, params={"s": symbol, "region": "US", "lang": "en-US"}, ttl=900)
        return parse_rss(text)

    def google(self, query: str) -> list[dict[str, Any]]:
        text = self.http.get_text(
            GOOGLE_RSS + "?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en", ttl=900
        )
        return parse_rss(text)
