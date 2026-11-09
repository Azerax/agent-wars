"""Grading catalysts from headlines and SEC filings.

A low-priced stock that doubles in four days almost always has a reason:
an FDA decision, a contract, a buyout. One that collapses usually has one
too: a share offering, a reverse split, a delisting notice. The lexicon
grader below always runs; Claude (llm.py) refines it when configured.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

POSITIVE: list[tuple[str, float, str]] = [
    (r"\bfda (?:grants? )?approv", 3.0, "fda_approval"),
    (r"\b(?:510\(k\)|de novo) clearance|\bfda clear", 2.0, "fda_clearance"),
    (r"breakthrough (?:therapy|device) designation|fast track designation|orphan drug designation", 1.5, "fda_designation"),
    (r"met (?:its )?primary endpoint|positive (?:topline|top-line|phase \d|clinical) (?:data|results)|statistically significant", 2.5, "positive_data"),
    (r"(?:to be acquired|definitive (?:merger )?agreement to be acquired|agrees? to be acquired|buyout offer|tender offer)", 3.0, "acquisition_target"),
    (r"\b(?:awarded|wins?|won|secures?|secured|lands?|landed|receives?|received)\b(?:\s+\S+){0,5}?\s+(?:contract|order|grant)s?\b", 2.0, "contract"),
    (r"\bcontract\b|purchase order|supply agreement", 1.0, "contract"),
    (r"strategic (?:partnership|collaboration|alliance)|partners? with|collaboration agreement|licensing agreement", 1.0, "partnership"),
    (r"record (?:quarterly |annual )?revenue|revenue (?:grew|up|increase[sd]) \d", 1.5, "growth"),
    (r"\b(?:beats?|tops?|exceeds?) (?:consensus|estimates|expectations)", 1.0, "earnings_beat"),
    (r"raises? (?:full[- ]year )?guidance|raises? outlook", 2.0, "guidance_raise"),
    (r"(?:share )?(?:buyback|repurchase program)", 1.0, "buyback"),
    (r"regain(?:s|ed)? compliance", 1.0, "compliance_regained"),
    (r"uplist", 1.0, "uplisting"),
    (r"\bpatent (?:granted|issued|allowance)", 0.5, "patent"),
    (r"short squeeze|heavily shorted", 0.5, "squeeze_chatter"),
]

NEGATIVE: list[tuple[str, float, str]] = [
    (r"(?:public|registered direct|underwritten|secondary|follow-on) offering|prices? (?:a |its )?offering|offering of (?:common|units|shares)", -3.0, "offering"),
    (r"private placement|at[- ]the[- ]market|\batm (?:program|facility)|equity line|shelf registration", -2.0, "dilution"),
    (r"\bwarrants?\b.*(?:exercise|inducement)|warrant inducement", -1.5, "warrants"),
    (r"reverse (?:stock )?split", -2.5, "reverse_split"),
    (r"delist|deficiency (?:notice|letter)|non-?compliance|minimum bid price", -2.0, "delisting_risk"),
    (r"chapter 11|chapter 7|bankruptcy|insolven", -4.0, "bankruptcy"),
    (r"going concern", -2.5, "going_concern"),
    (r"complete response letter|\bcrl\b|fda (?:rejects|declines)|clinical hold|failed to meet|did not meet", -3.0, "fda_setback"),
    (r"\b(?:misses|missed|falls short of) (?:consensus|estimates|expectations)", -1.0, "earnings_miss"),
    (r"(?:lowers|cuts|withdraws) (?:its )?(?:guidance|outlook)", -2.0, "guidance_cut"),
    (r"class action|securities fraud|sec investigation|subpoena|short seller report", -1.5, "legal"),
    (r"trading halt|halted", -0.5, "halt"),
    (r"(?:ceo|cfo|chief executive|chief financial).{0,30}(?:resign|steps down|terminated)", -1.0, "exec_departure"),
    (r"restatement|non-reliance", -2.0, "accounting"),
]

# 8-K item numbers and what they usually mean for the stock.
SEC_ITEMS: dict[str, tuple[float, str]] = {
    "1.01": (1.0, "material_agreement"),
    "1.02": (-1.0, "agreement_terminated"),
    "1.03": (-5.0, "bankruptcy"),
    "2.01": (0.5, "acquisition_completed"),
    "3.01": (-2.5, "delisting_notice"),
    "3.02": (-2.0, "unregistered_equity_sale"),
    "3.03": (-1.0, "holder_rights_modified"),
    "4.01": (-1.0, "auditor_change"),
    "4.02": (-2.5, "non_reliance"),
    "5.02": (-0.3, "officer_change"),
    "7.01": (0.3, "reg_fd"),
    "8.01": (0.5, "other_events"),
}

DILUTION_TAGS = {"offering", "dilution", "warrants", "unregistered_equity_sale", "prospectus"}

_COMPILED_POS = [(re.compile(p, re.I), w, t) for p, w, t in POSITIVE]
_COMPILED_NEG = [(re.compile(p, re.I), w, t) for p, w, t in NEGATIVE]


@dataclass
class CatalystAssessment:
    symbol: str
    score: float = 0.0  # -5 (disaster) .. +5 (huge catalyst)
    tags: list[str] = field(default_factory=list)
    dilution_risk: bool = False
    grader: str = "lexicon"
    summary: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "score": self.score, "tags": self.tags, "dilution_risk": self.dilution_risk,
            "grader": self.grader, "summary": self.summary, "evidence": self.evidence,
        }


def score_headline(title: str) -> tuple[float, list[str]]:
    score = 0.0
    tags: list[str] = []
    for rx, w, tag in _COMPILED_POS:
        if rx.search(title) and tag not in tags:
            score += w
            tags.append(tag)
    for rx, w, tag in _COMPILED_NEG:
        if rx.search(title) and tag not in tags:
            score += w
            tags.append(tag)
    return score, tags


def assess(
    symbol: str,
    headlines: list[dict[str, Any]],
    filings: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    max_age_days: float = 5,
) -> CatalystAssessment:
    """Combine recent headlines and filings into one score. Newer news counts more."""
    now = now or datetime.now(timezone.utc)
    total = 0.0
    tags: list[str] = []
    evidence: list[str] = []
    for h in headlines:
        pub = h.get("published")
        age_days = (now - pub).total_seconds() / 86400 if pub else 2.0
        if age_days > max_age_days or age_days < -1:
            continue
        s, t = score_headline(h.get("title", ""))
        if not t:
            continue
        decay = 0.5 ** (max(age_days, 0) / 2.0)
        total += s * decay
        for tag in t:
            if tag not in tags:
                tags.append(tag)
        evidence.append(f"{s:+.1f} {h.get('title', '')[:140]}")
    for f in filings or []:
        if f.get("dilution"):
            total -= 2.0
            if "prospectus" not in tags:
                tags.append("prospectus")
            evidence.append(f"-2.0 SEC {f['form']} filed (share offering risk)")
        for item in f.get("items", []):
            w, tag = SEC_ITEMS.get(item, (0.0, ""))
            if tag:
                total += w
                if tag not in tags:
                    tags.append(tag)
                evidence.append(f"{w:+.1f} SEC {f['form']} item {item} ({tag})")
    score = max(-5.0, min(5.0, total))
    return CatalystAssessment(
        symbol=symbol,
        score=round(score, 2),
        tags=tags,
        dilution_risk=bool(DILUTION_TAGS.intersection(tags)),
        summary="; ".join(evidence[:4]),
        evidence=evidence,
    )


def input_hash(headlines: list[dict[str, Any]], filings: list[dict[str, Any]] | None) -> str:
    h = hashlib.sha256()
    for x in headlines:
        h.update((x.get("title") or "").encode())
    for f in filings or []:
        h.update(f"{f.get('form')}{f.get('items')}{f.get('url')}".encode())
    return h.hexdigest()[:16]


def recent_filings_for(symbol: str, filings: list[dict[str, Any]], now: datetime, days: int = 30) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    out = []
    for f in filings:
        if f.get("symbol") != symbol:
            continue
        upd = f.get("updated")
        if upd is not None and upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
        if upd is None or upd >= cutoff:
            out.append(f)
    return out
