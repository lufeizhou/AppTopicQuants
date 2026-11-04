"""Can a brand-new app (0 ratings) get onto page one? Count the top-10 results that are weak themselves.

RightMeal/SparkNote analysis (2026-11-02): Astro's difficulty disagreed with reality ('calorie tracker photo':
Astro 23, yet the weakest top-10 app had 3,070 ratings). The number of top-10 apps with < 100 ratings was the
direct, verifiable signal of where a new app can rank."""
from __future__ import annotations

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


@metrics.register()
class WeakSpots(Metric):
    name = "weak_spots"
    description = "Number of Apple's top-10 results with < 100 ratings: spots a new app can take."
    requires = ("search_results",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        d = latest_snapshot(con, "search_results", "store", store, snapshot_date)
        if d is None:
            return []
        rows = con.execute("""
            SELECT keyword, sum(CASE WHEN coalesce(rating_count,0) < 100 THEN 1 ELSE 0 END), min(coalesce(rating_count,0)),
                   median(coalesce(rating_count,0)), count(*)
            FROM sr_latest WHERE store=? AND snapshot_date=? AND rank<=10 GROUP BY keyword""", [store, d]).fetchall()
        return [FactorRow(k, float(w), {"weakest_ratings": int(mn), "median_top10_ratings": int(md), "top_n": n})
                for k, w, mn, md, n in rows]
