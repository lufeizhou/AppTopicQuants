"""Google Play signals for the same keyword: real install counts of the top
results (demand cross-check) and the share that monetize via IAP."""
from __future__ import annotations

import math
import statistics

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


def _rows(con, country, snapshot_date):
    d = latest_snapshot(con, "play_search", "country", country, snapshot_date)
    if d is None:
        return []
    return con.execute("""
        SELECT s.keyword, list(a.min_installs ORDER BY s.rank), list(a.offers_iap ORDER BY s.rank),
               list(a.free ORDER BY s.rank)
        FROM play_search s LEFT JOIN (
            SELECT * FROM play_apps WHERE country=? QUALIFY row_number() OVER (PARTITION BY package ORDER BY snapshot_date DESC)=1
        ) a USING (package)
        WHERE s.country=? AND s.snapshot_date=? AND s.rank<=10 GROUP BY s.keyword""", [country, country, d]).fetchall()


@metrics.register()
class PlayDemand(Metric):
    name = "play_demand"
    description = "log10 median installs of top-10 Google Play results (real install counts)."
    requires = ("play_search", "play_apps")
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        out = []
        for kw, installs, _, _ in _rows(con, store, snapshot_date):
            vals = [i for i in installs if i]
            if not vals:
                continue
            med = statistics.median(vals)
            out.append(FactorRow(kw, round(math.log10(med + 1), 4), {"median_installs": med, "n": len(vals)}))
        return out


@metrics.register()
class PlayIapShare(Metric):
    name = "play_iap_share"
    description = "Share of top-10 Play results that are paid or offer in-app purchases."
    requires = ("play_search", "play_apps")
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        out = []
        for kw, _, iap, free in _rows(con, store, snapshot_date):
            pairs = [(i, f) for i, f in zip(iap, free) if i is not None or f is not None]
            if not pairs:
                continue
            share = sum(1 for i, f in pairs if i or (f is False)) / len(pairs)
            out.append(FactorRow(kw, round(share, 3), {"n": len(pairs)}))
        return out
