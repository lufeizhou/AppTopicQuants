from __future__ import annotations

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


@metrics.register()
class Demand(Metric):
    name = "demand"
    description = "Apple search popularity (5-100): Apple Ads weekly list; long-tail terms from Astro (tracked keywords)."
    requires = ("keywords",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        d = latest_snapshot(con, "keywords", "store", store, snapshot_date)
        if d is None:
            return []
        rows = con.execute("""
            WITH m AS (SELECT keyword, popularity FROM keyword_metrics WHERE store=? AND popularity IS NOT NULL
                       QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1)
            SELECT k.keyword, coalesce(k.popularity, m.popularity), k.genre, k.rank_in_store,
                   CASE WHEN k.popularity IS NULL AND m.popularity IS NOT NULL THEN 'astro' ELSE 'apple_ads' END
            FROM keywords k LEFT JOIN m USING (keyword) WHERE k.store=? AND k.snapshot_date=?""",
            [store, store, d]).fetchall()
        return [FactorRow(k, float(p), {"genre": g, "rank": r, "as_of": str(d), "source": src})
                for k, p, g, r, src in rows if p is not None]
