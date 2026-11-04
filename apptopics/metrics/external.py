"""Factors from off-store sources: Google Ads volume/CPC, Google Trends, Reddit, X,
Apple reviews (non-LLM part), Google Play charts."""
from __future__ import annotations

import json
import math

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot


def social_rows(con, store: str) -> dict[str, dict]:
    """keyword -> latest social evidence: its own keyword_social row (research runs) or, failing that, the row of a
    topic it belongs to (topic_social). views = TikTok + YouTube views of the matching videos."""
    out: dict[str, dict] = {}
    for kw, views, med, ads_c, ads_v, days in con.execute("""SELECT keyword, views, median_views, ads_checked, ads_verified, ads_oldest_days
            FROM keyword_social QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY fetched_date DESC, views DESC NULLS LAST)=1""").fetchall():
        out[kw] = {"views": views, "median": med, "ads_checked": ads_c, "ads_verified": ads_v, "ads_days": days, "src": "research"}
    for kw, views, med, ads_c, ads_v, days in con.execute("""
            WITH t AS (SELECT * FROM topic_social QUALIFY row_number() OVER (PARTITION BY topic_id ORDER BY fetched_date DESC)=1)
            SELECT k.keyword, CASE WHEN t.tiktok_views IS NULL AND t.youtube_views IS NULL THEN NULL ELSE coalesce(t.tiktok_views,0)+coalesce(t.youtube_views,0) END,
                   greatest(coalesce(t.tiktok_median_views,0), coalesce(t.youtube_median_views,0)),
                   t.apps_checked, t.apps_verified_ads, t.max_oldest_active_days
            FROM t JOIN topic_keywords k ON k.topic_id=t.topic_id AND k.store=?""", [store]).fetchall():
        if kw not in out:
            out[kw] = {"views": views, "median": med, "ads_checked": ads_c, "ads_verified": ads_v, "ads_days": days, "src": "topic"}
    return out


@metrics.register()
class SocialViews(Metric):
    name = "social_views"
    description = "log10 TikTok + YouTube views of videos about the need (ScrapeCreators; research aspects and sampled topics). People care even when they do not search the store yet."
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        return [FactorRow(kw, round(math.log10((r["views"] or 0) + 1), 4), {"views": r["views"], "median_views": r["median"], "source": r["src"]})
                for kw, r in social_rows(con, store).items() if r["views"] is not None]


@metrics.register()
class AdActivity(Metric):
    name = "ad_activity"
    description = ("Share of the market's top apps that keep paying for Meta ads (verified: the ad links to that app), weighted by how long "
                   "the ads have run (90+ days = full weight). Advertisers only keep paying when users are worth more than the ads.")
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        out = []
        for kw, r in social_rows(con, store).items():
            if not r["ads_checked"]:
                continue
            longevity = min(1.0, (r["ads_days"] or 0) / 90)
            share = (r["ads_verified"] or 0) / r["ads_checked"]
            out.append(FactorRow(kw, round(share * (0.5 + 0.5 * longevity), 4),
                                 {"apps_checked": r["ads_checked"], "apps_verified": r["ads_verified"], "oldest_days": r["ads_days"], "source": r["src"]}))
        return out


@metrics.register()
class SearchVolume(Metric):
    name = "search_volume"
    description = "log10 Google monthly search volume (Google Ads, US)."
    requires = ("keyword_volume",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        rows = con.execute("""SELECT keyword, search_volume, competition_index, monthly FROM keyword_volume WHERE snapshot_date<=?
                              QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [snapshot_date]).fetchall()
        out = []
        for kw, vol, ci, monthly in rows:
            if vol is None:
                continue
            m = json.loads(monthly or "[]")
            vals = [x.get("search_volume") or 0 for x in sorted(m, key=lambda x: (x.get("year", 0), x.get("month", 0)))]
            yoy = None
            if len(vals) >= 12 and sum(vals[:6]):
                yoy = round(sum(vals[-6:]) / sum(vals[:6]) - 1, 3)
            out.append(FactorRow(kw, round(math.log10(vol + 1), 4), {"volume": vol, "ads_competition_index": ci, "yoy_6m": yoy}))
        return out


@metrics.register()
class Cpc(Metric):
    name = "cpc"
    description = "Google Ads cost per click (USD): advertisers pay where buyers are."
    requires = ("keyword_volume",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        rows = con.execute("""SELECT keyword, cpc, competition FROM keyword_volume WHERE snapshot_date<=? AND cpc IS NOT NULL
                              QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [snapshot_date]).fetchall()
        return [FactorRow(kw, round(float(c), 3), {"ads_competition": comp}) for kw, c, comp in rows]


@metrics.register()
class TrendSlope(Metric):
    name = "trend_slope"
    description = "Google Trends relative change over the last ~12 months (+0.5 = up 50% vs mean level)."
    requires = ("trends",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        rows = con.execute("""SELECT keyword, slope_12m, rel_to_reference, reference, points FROM trends WHERE snapshot_date<=?
                              QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [snapshot_date]).fetchall()
        out = []
        for kw, s, rel, ref, pts in rows:
            extra = {}
            try:
                pj = json.loads(pts) if pts else None
                if isinstance(pj, dict):
                    extra = {k: pj.get(k) for k in ("seasonality", "peak_month", "low_month", "growth_12m_vs_prior")}
            except (TypeError, ValueError):
                pass
            out.append(FactorRow(kw, round(float(s or 0), 4), {"rel_to_reference": rel, "reference": ref, **extra}))
        return out


@metrics.register()
class TrendLevel(Metric):
    name = "trend_level"
    description = "log10 Google Trends interest relative to the reference term (cross-keyword comparable)."
    requires = ("trends",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        rows = con.execute("""SELECT keyword, rel_to_reference FROM trends WHERE snapshot_date<=? AND rel_to_reference IS NOT NULL
                              QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [snapshot_date]).fetchall()
        return [FactorRow(kw, round(math.log10(float(rel) + 0.01), 4), {}) for kw, rel in rows]


@metrics.register()
class ReviewNegShare(Metric):
    name = "review_neg_share"
    description = "Share of recent 1-2 star reviews across the keyword's p5-p20 band apps (dissatisfied users = openings)."
    requires = ("reviews", "search_results")
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        from .sector import _target_cfg, band, sector_estimates
        _, (lo, hi), _ = _target_cfg()
        stats = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT app_id, count(*), sum(CASE WHEN rating<=2 THEN 1 ELSE 0 END) FROM reviews WHERE store=? GROUP BY app_id", [store]).fetchall()}
        out = []
        for kw, apps in sector_estimates(con, store, snapshot_date).items():
            b = [a for a in band(apps, lo, hi) if a.app_id in stats]
            n = sum(stats[a.app_id][0] for a in b)
            if n < 20:
                continue
            neg = sum(stats[a.app_id][1] for a in b)
            out.append(FactorRow(kw, round(neg / n, 3), {"n_reviews": n, "n_apps": len(b)}))
        return out


@metrics.register()
class PlayGrossingPresence(Metric):
    name = "play_grossing"
    description = "Share of top-10 Google Play results present in any Play top-grossing list (second-store monetization proof)."
    requires = ("play_search", "play_charts")
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        d = latest_snapshot(con, "play_search", "country", store, snapshot_date)
        cd = latest_snapshot(con, "play_charts", "country", store, snapshot_date)
        if d is None or cd is None:
            return []
        grossing = {r[0]: r[1] for r in con.execute(
            "SELECT package, min(rank) FROM play_charts WHERE country=? AND collection='GROSSING' AND snapshot_date=? GROUP BY package", [store, cd]).fetchall()}
        rows = con.execute("SELECT keyword, list(package ORDER BY rank) FROM play_search WHERE country=? AND snapshot_date=? AND rank<=10 GROUP BY keyword",
                           [store, d]).fetchall()
        out = []
        for kw, pkgs in rows:
            hits = [p for p in pkgs if p in grossing]
            out.append(FactorRow(kw, round(len(hits) / max(1, len(pkgs)), 3), {"n_grossing": len(hits), "best_rank": min((grossing[p] for p in hits), default=None)}))
        return out



@metrics.register()
class Trending(Metric):
    name = "trending"
    description = ("Apple weekly popularity (1-100): latest week minus the mean of weeks it was seen in the prior 12 weeks. "
                   "Apple lists only the top 500 per genre, so terms near the cut-off drop in and out; absent weeks are not "
                   "zeros. A term never seen before is compared with its genre's cut-off that week.")
    requires = ("popularity_weekly",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        rows = con.execute("""
            WITH w AS (SELECT term, genre, week, max(pop_1to100) pop FROM popularity_weekly WHERE country=? AND week<=? GROUP BY term, genre, week),
                 last AS (SELECT max(week) wk FROM w),
                 floor AS (SELECT genre, min(pop) fl FROM w, last WHERE week = last.wk GROUP BY genre),
                 cur AS (SELECT term, max(pop) pop, any_value(genre) genre FROM w, last WHERE week = last.wk GROUP BY term),
                 prev AS (SELECT term, avg(pop) pop, count(DISTINCT week) n FROM w, last
                          WHERE week < last.wk AND week >= last.wk - INTERVAL 84 DAY GROUP BY term)
            SELECT cur.term, cur.pop, prev.pop, prev.n, floor.fl
            FROM cur LEFT JOIN prev USING (term) LEFT JOIN floor USING (genre)""", [store, snapshot_date]).fetchall()
        out = []
        for term, pop, prev, n, fl in rows:
            if prev is None:   # new to the list: it was below the genre cut-off -> at most (pop - cut-off)
                base = float(fl or pop)
                out.append(FactorRow(term, round(float(pop or 0) - base, 3), {"new_to_list": True, "pop": pop, "genre_cutoff": fl}))
            else:
                out.append(FactorRow(term, round(float(pop or 0) - float(prev), 3),
                                     {"new_to_list": False, "pop": pop, "prev_12w_mean": round(prev, 1), "weeks_seen": n}))
        return out
