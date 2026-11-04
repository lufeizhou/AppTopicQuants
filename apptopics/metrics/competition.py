"""Incumbent strength on the keyword's first results page."""
from __future__ import annotations

import math
import statistics

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


@metrics.register()
class Competition(Metric):
    name = "competition"
    description = "log10 of the median rating count of the top-10 results; higher = stronger incumbents."
    requires = ("search_results",)
    higher_is_better = False

    def compute(self, con, store, snapshot_date):
        d = latest_snapshot(con, "search_results", "store", store, snapshot_date)
        if d is None:
            return []
        rows = con.execute("""
            SELECT keyword, list(rating_count ORDER BY rank), list(avg_rating ORDER BY rank), max(total_results)
            FROM sr_latest WHERE store=? AND snapshot_date=? AND rank<=10 GROUP BY keyword""",
            [store, d]).fetchall()
        out = []
        for kw, counts, ratings, _total in rows:
            counts = [c or 0 for c in counts]
            med = statistics.median(counts) if counts else 0
            big = sum(1 for c in counts if c >= 10_000) / max(1, len(counts))
            weak = sum(1 for r, c in zip(ratings, counts) if (r or 0) < 4.0 and c > 100) / max(1, len(counts))
            val = math.log10(med + 1)
            out.append(FactorRow(kw, round(val, 4), {"median_ratings": med, "share_over_10k": round(big, 2),
                                                    "share_weak_rated": round(weak, 2), "n": len(counts)}))
        return out
