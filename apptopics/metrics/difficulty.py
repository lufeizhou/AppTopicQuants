"""Keyword difficulty 0-100: how hard it is to rank on the first page.

Computed from the sector itself (no vendor needed): incumbents' size, their maturity, and how
many heavyweights sit in the top 10. If Astro's tracked difficulty exists for the keyword it is
blended in at 25% (own measured data 75%: Astro proved unreliable on long-tail terms) and recorded in meta."""
from __future__ import annotations

import datetime as dt
import math

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


def own_difficulty(rating_counts: list[int], ages_days: list[float | None]) -> float:
    counts = sorted((c or 0) for c in rating_counts)[::-1][:10]
    if not counts:
        return 0.0
    med = counts[len(counts) // 2]
    size = min(1.0, math.log10(med + 1) / 5.5)                       # 300k ratings median -> 1.0
    heavy = sum(1 for c in counts if c >= 10_000) / len(counts)       # share of heavyweights
    ages = [a for a in ages_days if a is not None]
    maturity = min(1.0, (sum(ages) / len(ages)) / (365 * 5)) if ages else 0.5   # 5-year-old incumbents -> 1.0
    return round(100 * (0.5 * size + 0.3 * heavy + 0.2 * maturity), 1)


@metrics.register()
class Difficulty(Metric):
    name = "difficulty"
    description = "0-100 difficulty to rank on page one: incumbents' size, heavyweight share, maturity (+ Astro when tracked)."
    requires = ("search_results",)
    higher_is_better = False

    def compute(self, con, store, snapshot_date):
        sd = latest_snapshot(con, "search_results", "store", store, snapshot_date)
        if sd is None:
            return []
        astro = {k: v for k, v in con.execute("""
            SELECT keyword, difficulty FROM keyword_metrics WHERE store=? AND snapshot_date<=? AND difficulty IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [store, snapshot_date]).fetchall()}
        rows = con.execute("""
            WITH a AS (SELECT app_id, release_date FROM apps WHERE store=? QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1)
            SELECT s.keyword, list(s.rating_count ORDER BY s.rank), list(a.release_date ORDER BY s.rank)
            FROM sr_latest s LEFT JOIN a USING (app_id)
            WHERE s.store=? AND s.snapshot_date=? AND s.rank<=10 GROUP BY s.keyword""", [store, store, sd]).fetchall()
        now = dt.datetime.combine(snapshot_date, dt.time())
        out = []
        for kw, counts, dates in rows:
            own = own_difficulty(counts, [((now - d).days if d else None) for d in dates])
            if kw in astro:
                val, src = round(0.75 * own + 0.25 * float(astro[kw]), 1), "own 75% + astro 25%"
            else:
                val, src = own, "own"
            out.append(FactorRow(kw, val, {"own": own, "astro": astro.get(kw), "source": src}))
        return out
