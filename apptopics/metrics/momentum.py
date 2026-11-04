"""Change in demand over time. Needs history: zero with has_history=false until
snapshots accumulate. Combines popularity delta (7d, 28d) with rating-velocity
of the top-10 incumbents (a proxy for download growth in the niche)."""
from __future__ import annotations

import datetime as dt

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


def _pop_at(con, store, on_or_before: dt.date) -> dict[str, int]:
    d = latest_snapshot(con, "keywords", "store", store, on_or_before)
    if d is None:
        return {}
    return dict(con.execute("SELECT keyword, popularity FROM keywords WHERE store=? AND snapshot_date=?",
                            [store, d]).fetchall())


@metrics.register()
class Momentum(Metric):
    name = "momentum"
    description = "Popularity change vs 7/28 days ago plus incumbents' rating velocity."
    requires = ("keywords",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        now = _pop_at(con, store, snapshot_date)
        if not now:
            return []
        d7 = _pop_at(con, store, snapshot_date - dt.timedelta(days=7))
        d28 = _pop_at(con, store, snapshot_date - dt.timedelta(days=28))
        # rating velocity: sum rating_count of top-10 now vs earliest snapshot within 28d
        vel: dict[str, float] = {}
        cur = latest_snapshot(con, "search_results", "store", store, snapshot_date)
        if cur:
            rows = con.execute("""
                WITH cur AS (SELECT keyword, sum(rating_count) s FROM search_results
                             WHERE store=? AND snapshot_date=? AND rank<=10 GROUP BY keyword),
                     old AS (SELECT keyword, sum(rating_count) s, snapshot_date FROM search_results
                             WHERE store=? AND snapshot_date BETWEEN ? AND ? AND rank<=10 GROUP BY keyword, snapshot_date
                             QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date)=1)
                SELECT cur.keyword, cur.s, old.s, old.snapshot_date FROM cur JOIN old USING (keyword)""",
                [store, cur, store, snapshot_date - dt.timedelta(days=28), cur - dt.timedelta(days=1)]).fetchall()
            for kw, s_now, s_old, od in rows:
                days = max(1, (cur - od).days)
                if s_old:
                    vel[kw] = ((s_now or 0) - s_old) / s_old / days * 30  # monthly growth rate
        out = []
        for kw, p in now.items():
            has = kw in d7 or kw in d28
            dp7 = p - d7.get(kw, p) if d7 else 0
            dp28 = p - d28.get(kw, p) if d28 else 0
            v = vel.get(kw, 0.0)
            val = 0.5 * dp7 + 0.25 * dp28 + 25 * max(-1.0, min(1.0, v))
            out.append(FactorRow(kw, round(val, 4), {"dpop_7d": dp7, "dpop_28d": dp28,
                                                     "rating_velocity_m": round(v, 4), "has_history": has or kw in vel}))
        return out
