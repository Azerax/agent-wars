"""Crowd chatter: StockTwits trending symbols and sentiment, and Reddit's
penny-stock forums. Chatter is a lead, never a reason to buy on its own: the
strategies that use it also require price and volume to confirm."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .http import HttpClient

STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols.json"
STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
REDDIT_HOT = "https://www.reddit.com/r/{sub}/hot.json"
REDDIT_RSS = "https://www.reddit.com/r/{sub}/hot/.rss"
REDDIT_UA = "stockagent-paper-trader/0.1 (read-only research)"
ATOM = "{http://www.w3.org/2005/Atom}"

# Upper-case words that show up in posts and are not tickers worth chasing.
STOPWORDS = {
    "A", "I", "AI", "ALL", "AM", "AN", "AND", "ANY", "ARE", "AS", "AT", "ATH", "ATM", "BE", "BIG", "BUY", "BY", "CAN",
    "CEO", "CFO", "DD", "DO", "EOD", "EPS", "ETF", "EV", "FDA", "FOR", "FUD", "GO", "GOOD", "HAS", "HOLD", "IMO", "IN",
    "IPO", "IS", "IT", "LOL", "MAY", "ME", "MOON", "MY", "NEW", "NO", "NOT", "NOW", "NYSE", "OF", "OK", "ON", "OR",
    "OTC", "OUT", "PM", "PR", "RSI", "SEC", "SELL", "SO", "THE", "TO", "UP", "US", "USA", "WE", "YOLO", "YOU", "PT",
    "CEOS", "LFG", "TA", "Q1", "Q2", "Q3", "Q4", "EDIT", "TLDR", "WSB", "NFA", "IV", "ER", "HOD", "LOD", "VWAP", "AH",
}

_CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str, universe: set[str] | None = None) -> dict[str, int]:
    """Count ticker mentions. Cashtags count double; bare upper-case words only
    count if they are real tickers in the universe."""
    counts: dict[str, int] = defaultdict(int)
    for m in _CASHTAG.findall(text or ""):
        sym = m.upper()
        if sym in STOPWORDS:
            continue
        if universe is None or sym in universe:
            counts[sym] += 2
    if universe:
        for m in _BARE.findall(_CASHTAG.sub(" ", text or "")):
            if m in universe and m not in STOPWORDS:
                counts[m] += 1
    return dict(counts)


def parse_stocktwits_trending(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for i, s in enumerate(payload.get("symbols") or []):
        sym = (s.get("symbol") or "").upper()
        if not sym or "." in sym:
            continue
        out.append({"symbol": sym, "rank": i + 1, "title": s.get("title"), "watchlist_count": s.get("watchlist_count")})
    return out


def parse_stocktwits_stream(payload: dict[str, Any]) -> dict[str, Any]:
    bull = bear = total = 0
    newest = None
    for m in payload.get("messages") or []:
        total += 1
        sentiment = ((m.get("entities") or {}).get("sentiment") or {}) or {}
        basic = (sentiment.get("basic") or "").lower()
        if basic == "bullish":
            bull += 1
        elif basic == "bearish":
            bear += 1
        created = m.get("created_at")
        if created and (newest is None or created > newest):
            newest = created
    tagged = bull + bear
    return {
        "messages": total,
        "bullish": bull,
        "bearish": bear,
        "bull_ratio": (bull / tagged) if tagged else None,
        "newest": newest,
    }


def parse_reddit_listing(payload: dict[str, Any]) -> list[dict[str, Any]]:
    posts = []
    for child in (payload.get("data") or {}).get("children") or []:
        d = child.get("data") or {}
        if d.get("stickied"):
            continue
        created = d.get("created_utc")
        posts.append(
            {
                "title": d.get("title", ""),
                "text": d.get("selftext", "") or "",
                "score": int(d.get("score") or 0),
                "comments": int(d.get("num_comments") or 0),
                "flair": d.get("link_flair_text"),
                "url": "https://www.reddit.com" + d["permalink"] if d.get("permalink") else d.get("url"),
                "created": datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None,
            }
        )
    return posts


def parse_reddit_rss(xml_text: str) -> list[dict[str, Any]]:
    """Reddit's Atom feed: no scores or comment counts, but rarely blocked."""
    import html as _html
    import xml.etree.ElementTree as ET_XML

    from .http import DataError

    try:
        root = ET_XML.fromstring(xml_text)
    except ET_XML.ParseError as exc:
        raise DataError(f"reddit rss: bad XML ({exc})") from exc
    posts = []
    for e in root.findall(f"{ATOM}entry"):
        link = e.find(f"{ATOM}link")
        body = re.sub(r"<[^>]+>", " ", _html.unescape(e.findtext(f"{ATOM}content") or ""))
        updated = e.findtext(f"{ATOM}updated") or e.findtext(f"{ATOM}published")
        try:
            created = datetime.fromisoformat(updated) if updated else None
        except ValueError:
            created = None
        posts.append({
            "title": (e.findtext(f"{ATOM}title") or "").strip(), "text": body[:2000], "score": 0, "comments": 0,
            "flair": None, "url": link.get("href") if link is not None else None, "created": created,
        })
    return posts


def rank_reddit_mentions(posts: Iterable[dict[str, Any]], universe: set[str]) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for p in posts:
        mentions = extract_tickers(f"{p['title']}\n{p['text'][:2000]}", universe)
        weight = 1.0 + min(p["score"], 500) / 100 + min(p["comments"], 300) / 150
        for sym, n in mentions.items():
            a = agg.setdefault(sym, {"symbol": sym, "mentions": 0, "weight": 0.0, "posts": []})
            a["mentions"] += n
            a["weight"] += n * weight
            if len(a["posts"]) < 3:
                a["posts"].append({"title": p["title"][:160], "url": p["url"], "score": p["score"]})
    return sorted(agg.values(), key=lambda a: -a["weight"])


class Social:
    def __init__(self, http: HttpClient):
        self.http = http

    def stocktwits_trending(self) -> list[dict[str, Any]]:
        return parse_stocktwits_trending(self.http.get_json(STOCKTWITS_TRENDING, ttl=300))

    def stocktwits_sentiment(self, symbol: str) -> dict[str, Any]:
        return parse_stocktwits_stream(self.http.get_json(STOCKTWITS_STREAM.format(symbol=symbol), ttl=600))

    def reddit_hot(self, sub: str, limit: int = 75) -> list[dict[str, Any]]:
        from .http import DataError

        headers = {"User-Agent": REDDIT_UA}
        try:
            return parse_reddit_listing(
                self.http.get_json(REDDIT_HOT.format(sub=sub), params={"limit": limit}, headers=headers, ttl=600)
            )
        except DataError as json_err:
            try:
                return parse_reddit_rss(
                    self.http.get_text(REDDIT_RSS.format(sub=sub), params={"limit": limit}, headers=headers, ttl=600)
                )
            except DataError as rss_err:
                raise DataError(f"reddit json: {json_err}; rss: {rss_err}") from rss_err
