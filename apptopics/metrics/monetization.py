"""Niche-level revenue proof: log10 of total estimated monthly revenue across the
sector (someone pays here at all). The per-app estimator lives in sector.py."""
from __future__ import annotations

import math

from ..registry import metrics
from .base import FactorRow, Metric


def rank_to_revenue(rank: int, a: float, b: float) -> float:
    return a * (rank ** (-b))


@metrics.register()
class Monetization(Metric):
    name = "monetization"
    description = "log10(total est. monthly USD of the sector); uncalibrated proxy."
    requires = ("search_results",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        from .sector import sector_estimates
        out = []
        for kw, apps in sector_estimates(con, store, snapshot_date).items():
            total = sum(a.est for a in apps)
            out.append(FactorRow(kw, round(math.log10(total + 1), 4),
                                 {"est_total_usd": round(total), "n": len(apps),
                                  "n_in_grossing": sum(1 for a in apps if a.src == "grossing")}))
        return out
