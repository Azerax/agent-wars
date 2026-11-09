"""The agent learns from its own record.

Two things adapt: how much each strategy is trusted (from the net-of-fees
R-multiples of its closed trades) and how much each tip source is trusted
(from what the tips it produced actually did over the next one and four days).
Both start from neutral priors and are shrunk toward them until there is
enough evidence, so one lucky trade does not rewrite the agent.
"""
from __future__ import annotations

import random
from typing import Any

from .config import AgentConfig
from .db import Database


def _shrink(value: float, n: int, prior: float, k: float) -> float:
    return (value * n + prior * k) / (n + k)


class StrategyLearner:
    PRIOR_WIN_R = 2.0
    PRIOR_LOSS_R = 1.0
    K = 5.0

    def __init__(self, db: Database, account_id: int | None, cfg: AgentConfig, seed: int | None = None):
        self.db = db
        self.account_id = account_id
        self.cfg = cfg
        self.rng = random.Random(seed)

    def stats(self) -> dict[str, dict[str, Any]]:
        rows = self.db.all(
            "SELECT strategy, r_multiple, net_pnl, fees FROM trades WHERE status='closed' AND account_id=? AND strategy IS NOT NULL",
            [self.account_id],
        )
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            s = out.setdefault(
                r["strategy"],
                {"trades": 0, "wins": 0, "losses": 0, "win_r": [], "loss_r": [], "net_pnl": 0.0, "fees": 0.0, "r": []},
            )
            s["trades"] += 1
            s["net_pnl"] += r["net_pnl"] or 0.0
            s["fees"] += r["fees"] or 0.0
            rm = r["r_multiple"]
            if rm is None:
                continue
            s["r"].append(rm)
            if (r["net_pnl"] or 0) > 0:
                s["wins"] += 1
                s["win_r"].append(rm)
            else:
                s["losses"] += 1
                s["loss_r"].append(abs(rm))
        for s in out.values():
            n = len(s["r"])
            s["win_rate"] = s["wins"] / n if n else None
            s["avg_r"] = sum(s["r"]) / n if n else None
            s["avg_win_r"] = sum(s["win_r"]) / len(s["win_r"]) if s["win_r"] else None
            s["avg_loss_r"] = sum(s["loss_r"]) / len(s["loss_r"]) if s["loss_r"] else None
            s["quarantined"] = bool(
                n >= self.cfg.quarantine_min_trades and s["avg_r"] is not None and s["avg_r"] < self.cfg.quarantine_r_floor
            )
            for k in ("win_r", "loss_r", "r"):
                del s[k]
        return out

    def weights(self, names: list[str], sample: bool = True) -> dict[str, float]:
        """Thompson sampling on win rate, times shrunk payoff: explores
        untested strategies, leans on proven ones, benches losers."""
        if not self.cfg.adaptive:
            return {n: 1.0 for n in names}
        stats = self.stats()
        out = {}
        for name in names:
            s = stats.get(name)
            if s is None:
                p = self.rng.betavariate(1, 1) if sample else 0.5
                w_r, l_r = self.PRIOR_WIN_R, self.PRIOR_LOSS_R
            else:
                if s["quarantined"]:
                    out[name] = 0.0
                    continue
                p = self.rng.betavariate(1 + s["wins"], 1 + s["losses"]) if sample else (1 + s["wins"]) / (2 + s["wins"] + s["losses"])
                w_r = _shrink(s["avg_win_r"] or 0.0, s["wins"], self.PRIOR_WIN_R, self.K)
                l_r = _shrink(s["avg_loss_r"] or 0.0, s["losses"], self.PRIOR_LOSS_R, self.K)
            exp_r = p * w_r - (1 - p) * l_r
            out[name] = round(max(0.05, min(2.0, 0.5 + exp_r)), 4)
        return out


class SourceLearner:
    K = 10.0

    def __init__(self, db: Database):
        self.db = db

    def stats(self) -> dict[str, dict[str, Any]]:
        rows = self.db.all(
            "SELECT source, COUNT(*) AS tips, SUM(eligible) AS eligible, "
            "SUM(evaluated_1d) AS n1, AVG(CASE WHEN evaluated_1d=1 THEN MIN(MAX(fwd_1d,-0.5),1.0) END) AS avg1, "
            "AVG(CASE WHEN evaluated_1d=1 THEN (fwd_1d>0) END) AS hit1, "
            "SUM(evaluated_4d) AS n4, AVG(CASE WHEN evaluated_4d=1 THEN MIN(MAX(fwd_4d,-0.8),3.0) END) AS avg4, "
            "SUM(CASE WHEN evaluated_4d=1 AND fwd_4d>=1.0 THEN 1 ELSE 0 END) AS doubled4 "
            "FROM tips GROUP BY source ORDER BY source"
        )
        return {
            r["source"]: {
                "tips": r["tips"], "eligible": r["eligible"] or 0, "evaluated_1d": r["n1"] or 0,
                "avg_fwd_1d": r["avg1"], "hit_rate_1d": r["hit1"], "evaluated_4d": r["n4"] or 0,
                "avg_fwd_4d": r["avg4"], "doubled_in_4d": r["doubled4"] or 0,
            }
            for r in rows
        }

    def weights(self) -> dict[str, float]:
        out = {}
        for source, s in self.stats().items():
            n = s["evaluated_1d"]
            avg = s["avg_fwd_1d"] or 0.0
            shrunk = _shrink(avg, n, 0.0, self.K)
            out[source] = round(max(0.2, min(2.0, 1.0 + 8.0 * shrunk)), 4)
        return out

    def weight(self, source: str, weights: dict[str, float] | None = None) -> float:
        weights = weights if weights is not None else self.weights()
        return weights.get(source, 1.0)
