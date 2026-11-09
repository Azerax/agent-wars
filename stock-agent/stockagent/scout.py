"""The scout finds its own tips.

It sweeps market movers, the whole-market screener, crowd chatter and SEC
filings, records every tip it sees (eligible or not) with the price at the
time, and later scores each tip by what the stock actually did. Sources that
keep producing losers lose weight; that is how the agent learns whom to
listen to.
"""
from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from . import catalysts
from . import clock as mcal
from .config import Config
from .data.http import DataError
from .data.provider import MarketData
from .data.social import rank_reddit_mentions
from .db import Database, dumps
from .learning import SourceLearner
from .llm import CatalystLLM

log = logging.getLogger(__name__)


class Scout:
    # Symbols per scan that get Alpha Vantage news sentiment (each costs a call).
    SENTIMENT_TOP = 3
    def __init__(
        self,
        cfg: Config,
        db: Database,
        md: MarketData,
        clock: mcal.Clock,
        llm: CatalystLLM | None = None,
        sources: SourceLearner | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.md = md
        self.clock = clock
        self.llm = llm
        self.sources = sources or SourceLearner(db)
        self.universe_df: pd.DataFrame | None = None
        self._uindex: dict[str, dict[str, Any]] = {}
        self.filings: list[dict[str, Any]] = []
        self.source_errors: dict[str, str] = {}
        self._exclude = [re.compile(p) for p in cfg.universe.exclude_symbol_patterns]

    # --------------------------------------------------------------- filters
    def eligible(self, symbol: str, price: float | None, volume: float | None = None) -> tuple[bool, str]:
        u = self.cfg.universe
        for rx in self._exclude:
            if rx.search(symbol):
                return False, "not a common stock symbol"
        if price is None or not math.isfinite(price):
            return False, "no price"
        if price < u.min_price:
            return False, f"price {price:.4f} below ${u.min_price}"
        if price > u.max_price:
            return False, f"price {price:.2f} above ${u.max_price}"
        if volume is not None and volume < u.min_volume:
            return False, f"volume {volume:,.0f} below {u.min_volume:,}"
        return True, ""

    def _universe_row(self, symbol: str) -> dict[str, Any] | None:
        return self._uindex.get(symbol)

    def _universe_set(self) -> set[str]:
        if self.universe_df is None or self.universe_df.empty:
            return set()
        return set(self.universe_df["symbol"])

    # ------------------------------------------------------------- discovery
    def _tip(self, symbol: str, source: str, kind: str, raw: float, price: float | None = None,
             volume: float | None = None, title: str | None = None, url: str | None = None,
             payload: dict | None = None) -> dict[str, Any]:
        row = self._universe_row(symbol)
        if price is None and row:
            price = row.get("price")
        if volume is None and row:
            volume = row.get("volume")
        ok, why = self.eligible(symbol, price, volume)
        if ok and self.universe_df is not None and not self.universe_df.empty and row is None:
            ok, why = False, "not in listed universe (OTC or unknown)"
        return {
            "symbol": symbol, "source": source, "kind": kind, "raw_score": round(float(raw), 4),
            "price_at_tip": price, "eligible": ok, "reject_reason": why or None, "title": title, "url": url,
            "payload": payload or {},
        }

    def discover(self) -> list[dict[str, Any]]:
        self.source_errors = {}
        sources = set(self.cfg.data.sources)
        tips: list[dict[str, Any]] = []
        try:
            self.universe_df = self.md.universe()
        except DataError as exc:
            self.source_errors["universe"] = str(exc)
            self.universe_df = None
        self._uindex = (
            {r["symbol"]: r for r in self.universe_df.to_dict("records")}
            if self.universe_df is not None and not self.universe_df.empty else {}
        )

        if "yahoo_screeners" in sources:
            try:
                for r in self.md.screener_movers():
                    chg = (r.get("change_pct") or 0.0) / 100
                    rv = (r.get("volume") or 0) / r["avg_volume"] if r.get("avg_volume") else 1.0
                    raw = max(0.0, min(1.0, chg / 0.3)) * 0.6 + min(rv / 5, 1.0) * 0.4
                    tips.append(self._tip(
                        r["symbol"], f"yahoo:{r.get('screener', 'screener')}", "mover", raw, r.get("price"),
                        r.get("volume"), r.get("name"),
                        payload={k: r.get(k) for k in ("change_pct", "volume", "avg_volume", "market_cap", "shares_outstanding")},
                    ))
            except DataError as exc:
                self.source_errors["yahoo_screeners"] = str(exc)

        if "nasdaq_movers" in sources and self.universe_df is not None and not self.universe_df.empty:
            u = self.cfg.universe
            df = self.universe_df.dropna(subset=["price", "volume"])
            df = df[(df["price"] >= u.min_price) & (df["price"] <= u.max_price) & (df["volume"] >= u.min_volume)]
            gainers = df[df["change_pct"].fillna(0) >= 5].sort_values("change_pct", ascending=False).head(30)
            for _, r in gainers.iterrows():
                tips.append(self._tip(
                    r["symbol"], "nasdaq:gainers", "mover", min(float(r["change_pct"]) / 30, 1.0), float(r["price"]),
                    float(r["volume"]), r.get("name"), payload={"change_pct": r["change_pct"], "volume": r["volume"]},
                ))
            active = df.assign(dv=df["price"] * df["volume"]).sort_values("dv", ascending=False).head(20)
            for _, r in active.iterrows():
                tips.append(self._tip(
                    r["symbol"], "nasdaq:active", "volume", min(float(r["dv"]) / 50e6, 1.0), float(r["price"]),
                    float(r["volume"]), r.get("name"), payload={"dollar_volume": r["dv"], "change_pct": r["change_pct"]},
                ))

        if "alphavantage" in sources:
            try:
                for r in self.md.alt_movers():
                    if r["list"] == "losers":
                        continue
                    chg = (r.get("change_pct") or 0.0) / 100
                    raw = min(max(chg, 0.0) / 0.3, 1.0) if r["list"] == "gainers" else 0.5
                    tips.append(self._tip(
                        r["symbol"], f"alphavantage:{r['list']}", "mover", raw, r.get("price"), r.get("volume"),
                        payload={"change_pct": r.get("change_pct"), "volume": r.get("volume")},
                    ))
            except DataError as exc:
                self.source_errors["alphavantage"] = str(exc)

        if "stocktwits" in sources:
            try:
                for r in self.md.stocktwits_trending():
                    tips.append(self._tip(
                        r["symbol"], "stocktwits:trending", "social", max(0.0, 1 - (r["rank"] - 1) / 30),
                        title=r.get("title"), payload={"rank": r["rank"], "watchlist_count": r.get("watchlist_count")},
                    ))
            except DataError as exc:
                self.source_errors["stocktwits"] = str(exc)

        if "reddit" in sources:
            uni = self._universe_set()
            for sub in self.cfg.data.reddit_subs:
                try:
                    posts = self.md.reddit_posts(sub)
                except DataError as exc:
                    self.source_errors[f"reddit:{sub}"] = str(exc)
                    continue
                for r in rank_reddit_mentions(posts, uni)[:25]:
                    top = r["posts"][0] if r["posts"] else {}
                    tips.append(self._tip(
                        r["symbol"], f"reddit:{sub}", "social", min(r["weight"] / 20, 1.0), title=top.get("title"),
                        url=top.get("url"), payload={"mentions": r["mentions"], "weight": r["weight"], "posts": r["posts"]},
                    ))

        if "sec_filings" in sources:
            try:
                self.filings = self.md.filings()
            except DataError as exc:
                self.source_errors["sec_filings"] = str(exc)
                self.filings = []
            for f in self.filings:
                if not f.get("symbol") or f.get("dilution"):
                    continue
                item_score = sum(catalysts.SEC_ITEMS.get(i, (0.0, ""))[0] for i in f.get("items", []))
                if item_score <= 0:
                    continue
                tips.append(self._tip(
                    f["symbol"], f"sec:{f['form']}", "filing", min(item_score / 3, 1.0),
                    title=f"{f['form']} items {', '.join(f['items'])}: {f['company']}", url=f.get("url"),
                    payload={"items": f["items"], "company": f["company"]},
                ))

        now = self.clock.now()
        today = now.date().isoformat()
        for t in tips:
            # A source re-reporting the same symbol the same day is one tip, priced
            # when first seen; counting it again would skew the source's record.
            seen = self.db.one(
                "SELECT id FROM tips WHERE day=? AND symbol=? AND source=?", [today, t["symbol"], t["source"]]
            )
            if seen:
                t["id"] = seen["id"]
                continue
            t["id"] = self.db.insert(
                "tips",
                {
                    "ts": now.isoformat(), "day": today, "symbol": t["symbol"], "source": t["source"], "kind": t["kind"],
                    "title": (t["title"] or None) and str(t["title"])[:300], "url": t["url"], "raw_score": t["raw_score"],
                    "price_at_tip": t["price_at_tip"], "eligible": int(t["eligible"]), "reject_reason": t["reject_reason"],
                    "payload": dumps(t["payload"]),
                },
            )
        for src, err in self.source_errors.items():
            self._journal("warn", "source_failed", None, f"{src} unavailable: {err[:200]}")
        return tips

    # ----------------------------------------------------------------- ranking
    def rank(self, tips: list[dict[str, Any]], enrich_top: int | None = None) -> list[dict[str, Any]]:
        weights = self.sources.weights()
        by_symbol: dict[str, dict[str, Any]] = {}
        for t in tips:
            if not t["eligible"]:
                continue
            c = by_symbol.setdefault(
                t["symbol"],
                {"symbol": t["symbol"], "score": 0.0, "sources": set(), "tip_ids": [], "price": t["price_at_tip"], "social": {}},
            )
            c["score"] += weights.get(t["source"], 1.0) * t["raw_score"]
            c["sources"].add(t["source"])
            c["tip_ids"].append(t["id"])
            if t["source"] == "stocktwits:trending":
                c["social"]["stocktwits_rank"] = t["payload"].get("rank")
            elif t["source"].startswith("reddit:"):
                c["social"]["reddit_weight"] = c["social"].get("reddit_weight", 0.0) + t["payload"].get("weight", 0.0)
        for c in by_symbol.values():
            c["score"] *= 1 + 0.25 * (len(c["sources"]) - 1)  # independent sources agreeing
        ranked = sorted(by_symbol.values(), key=lambda c: -c["score"])
        n = enrich_top if enrich_top is not None else self.cfg.agent.watchlist_size
        out = []
        for i, c in enumerate(ranked[:n]):
            self._enrich(c, sentiment=i < self.SENTIMENT_TOP)
            out.append(c)
        out.sort(key=lambda c: -c["final_score"])
        today = self.clock.now().date().isoformat()
        for c in out:
            self.db.upsert(
                "watchlist",
                {
                    "day": today, "symbol": c["symbol"], "score": c["final_score"], "sources": dumps(sorted(c["sources"])),
                    "catalyst_score": c["catalyst"].score, "dilution_risk": int(c["catalyst"].dilution_risk),
                    "data": dumps({"tip_ids": c["tip_ids"], "social": c["social"], "catalyst": c["catalyst"].to_dict(),
                                   "tip_score": c["score"]}),
                    "updated_at": self.clock.now().isoformat(),
                },
                ["day", "symbol"],
            )
        return out

    def _enrich(self, c: dict[str, Any], sentiment: bool = False) -> None:
        sym = c["symbol"]
        now = self.clock.now().astimezone(timezone.utc)
        headlines: list[dict[str, Any]] = []
        if "news" in self.cfg.data.sources:
            try:
                headlines = self.md.news(sym)
            except DataError as exc:
                c["news_error"] = str(exc)[:120]
        av = None
        if sentiment and "alphavantage" in self.cfg.data.sources:
            try:
                av = self.md.news_sentiment(sym)
            except DataError as exc:
                self.source_errors.setdefault("alphavantage_news", str(exc))
        if av:
            seen = {(h.get("title") or "").lower()[:90] for h in headlines}
            headlines += [h for h in av["headlines"] if (h.get("title") or "").lower()[:90] not in seen]
            c["news_sentiment"] = {k: av[k] for k in ("articles", "sentiment", "relevance")}
        filings = catalysts.recent_filings_for(sym, self.filings, now)
        lex = catalysts.assess(sym, headlines, filings, now)
        if av and av.get("sentiment") is not None and av.get("relevance", 0) >= 0.3:
            # Alpha Vantage sentiment runs roughly -0.35..+0.35 per article;
            # scale it onto the -5..+5 catalyst range, capped at +/-2.
            bump = max(-2.0, min(2.0, av["sentiment"] * 6))
            lex.score = round(max(-5.0, min(5.0, lex.score + bump)), 2)
            lex.tags.append("av_sentiment")
            lex.evidence.append(f"{bump:+.1f} Alpha Vantage sentiment {av['sentiment']:+.2f} over {av['articles']} articles")
            lex.summary = "; ".join(lex.evidence[:4])
        assessment = lex
        if self.llm is not None and self.llm.enabled and (headlines or filings):
            ih = catalysts.input_hash(headlines, filings)
            cached = self.db.one(
                "SELECT * FROM catalysts WHERE symbol=? AND input_hash=? AND grader LIKE 'claude%' ORDER BY id DESC LIMIT 1",
                [sym, ih],
            )
            if cached:
                assessment = catalysts.CatalystAssessment(
                    sym, cached["score"], (cached["tags"] or "").split(","), bool(cached["dilution_risk"]),
                    cached["grader"], cached["summary"] or "",
                )
            else:
                graded = self.llm.grade(sym, headlines, filings, self.clock.now().date())
                if graded is not None:
                    graded.score = round(0.7 * graded.score + 0.3 * lex.score, 2)
                    graded.dilution_risk = graded.dilution_risk or lex.dilution_risk
                    assessment = graded
                    self._store_catalyst(assessment, ih)
        if assessment is lex:
            self._store_catalyst(lex, catalysts.input_hash(headlines, filings))
        c["catalyst"] = assessment
        c["headlines"] = [h.get("title") for h in headlines[:5]]
        if c["social"].get("stocktwits_rank") or c["social"].get("reddit_weight"):
            try:
                c["social"].update({k: v for k, v in self.md.stocktwits_sentiment(sym).items() if k in ("bull_ratio", "messages")})
            except (DataError, NotImplementedError):
                pass
        final = c["score"] + 0.25 * assessment.score
        if assessment.dilution_risk:
            final *= 0.3
        c["final_score"] = round(final, 4)
        c["sources"] = c["sources"] if isinstance(c["sources"], set) else set(c["sources"])

    def _store_catalyst(self, a: catalysts.CatalystAssessment, ih: str) -> None:
        self.db.insert(
            "catalysts",
            {
                "ts": self.clock.now().isoformat(), "symbol": a.symbol, "grader": a.grader, "score": a.score,
                "dilution_risk": int(a.dilution_risk), "tags": ",".join(a.tags), "summary": a.summary[:500],
                "input_hash": ih,
            },
        )

    def run(self) -> list[dict[str, Any]]:
        tips = self.discover()
        candidates = self.rank(tips)
        eligible = sum(1 for t in tips if t["eligible"])
        self._journal(
            "info", "scout", None,
            f"{len(tips)} tips ({eligible} eligible) from {len({t['source'] for t in tips})} sources; "
            f"watchlist: {', '.join(c['symbol'] for c in candidates[:10]) or 'empty'}",
            {"errors": self.source_errors},
        )
        return candidates

    def held_catalysts(self, symbols: list[str]) -> dict[str, catalysts.CatalystAssessment]:
        out = {}
        now = self.clock.now().astimezone(timezone.utc)
        for sym in symbols:
            try:
                headlines = self.md.news(sym)
            except DataError:
                continue
            out[sym] = catalysts.assess(sym, headlines, catalysts.recent_filings_for(sym, self.filings, now), now, max_age_days=1)
        return out

    # -------------------------------------------------------------- follow-up
    def evaluate_tips(self, max_symbols: int = 60) -> int:
        """Score past tips by what happened next. Returns tips updated."""
        today = self.clock.now().date()
        last_closed = mcal.prev_trading_day(today) if mcal.market_phase(self.clock.now()) != "afterhours" else today
        rows = self.db.all(
            "SELECT id, day, symbol, price_at_tip, evaluated_1d, evaluated_4d FROM tips "
            "WHERE price_at_tip IS NOT NULL AND (evaluated_1d=0 OR evaluated_4d=0) AND day<? ORDER BY day DESC",
            [last_closed.isoformat()],
        )
        by_sym: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_sym[r["symbol"]].append(r)
        updated = 0
        for sym in list(by_sym)[:max_symbols]:
            try:
                df = self.md.daily_bars(sym, 40)
            except DataError:
                continue
            closes = {d.date(): float(c) for d, c in df["close"].items()}
            for r in by_sym[sym]:
                tip_day = date.fromisoformat(r["day"])
                changes: dict[str, Any] = {}
                d1 = mcal.next_trading_day(tip_day, 1)
                d4 = mcal.next_trading_day(tip_day, 4)
                base = r["price_at_tip"]
                if not r["evaluated_1d"] and d1 <= last_closed and d1 in closes and base:
                    changes.update(fwd_1d=round(closes[d1] / base - 1, 6), evaluated_1d=1)
                if not r["evaluated_4d"] and d4 <= last_closed and d4 in closes and base:
                    changes.update(fwd_4d=round(closes[d4] / base - 1, 6), evaluated_4d=1)
                if changes:
                    self.db.update("tips", r["id"], changes)
                    updated += 1
        return updated

    def _journal(self, level: str, event: str, symbol: str | None, message: str, data: dict | None = None) -> None:
        self.db.insert(
            "journal",
            {"ts": self.clock.now().isoformat(), "level": level, "event": event, "symbol": symbol,
             "message": message, "data": dumps(data or {})},
        )


def recent_reverse_split(splits: list[dict[str, Any]], today: date, days: int = 90) -> bool:
    cutoff = today - timedelta(days=days)
    return any(s["date"] >= cutoff and s["numerator"] < s["denominator"] for s in splits)


def tip_age_ok(ts: datetime, now: datetime, hours: float = 30) -> bool:
    return (now - ts).total_seconds() <= hours * 3600
