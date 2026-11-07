"""Step 3 market scores: four 0-100 indexes per search instead of a dollar guess (2026-11-06; v4 2026-11-07).

The per-app dollar estimate is within 3x of the truth only ~57% of the time and misses quiet high earners; no public
signal tested fixed that (CHANGELOG Step 3). These indexes say what the data can honestly say. v4 (user 2026-11-07):
every index is built by the general recipe in `indexing.py` (each ingredient -> percentile among all searches, weighted
mean, re-ranked, 5 blocks, also ranked within the search's App Store category) and shows its ingredients on hover.

Ingredients come from the search's real rivals (Step 2 kept) in the top 20 of its results page, plus search data:

  idx_popularity  do many people use these apps?   ratings per month of the rivals (x2) · rivals on a top-free chart (x1)
  idx_paying      do users pay?                     rivals on a top-grossing chart (x2) · leaders' monthly price (x1)
  idx_momentum    is it growing now?                Apple search popularity trend, 6 months · Google search volume trend,
                                                    12 months · rivals' rating pace now vs lifetime · young rivals
  idx_demand      how often is it searched?        Apple search popularity (x2) · Google monthly searches (x1)

Ingredient tests and the ones left out are in docs/CHANGELOG.md (Step 3, Round 4).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics

from ..registry import metrics
from .base import FactorRow, Metric
from .indexing import IndexSpec, Ingredient, build, why

TOP = 20
PER_MONTH = {"week": 52 / 12, "month": 1.0, "year": 1 / 12}
EARLY_DAYS = 28
GENRES = {"6000": "Business", "6001": "Weather", "6002": "Utilities", "6003": "Travel", "6004": "Sports", "6005": "Social Networking",
          "6006": "Reference", "6007": "Productivity", "6008": "Photo & Video", "6009": "News", "6010": "Navigation", "6011": "Music",
          "6012": "Lifestyle", "6013": "Health & Fitness", "6014": "Games", "6015": "Finance", "6016": "Entertainment", "6017": "Education",
          "6018": "Books", "6020": "Medical", "6021": "Magazines & Newspapers", "6022": "Catalogs", "6023": "Food & Drink", "6024": "Shopping",
          "6025": "Stickers", "6026": "Developer Tools", "6027": "Graphics & Design"}
_cache: dict[tuple, dict[str, dict]] = {}


def _pts(rank: int) -> float:
    """Chart points: #1 counts 1, #10 0.57, #100 0.13, #200 0."""
    return max(0.0, 1 - math.log10(rank) / math.log10(200))


SPECS = {
    "popularity": IndexSpec("idx_popularity", "Popularity", "Do many people use these apps?", [
        Ingredient("ratings_month", "Ratings per month", 2, "int", post=" new ratings/month across the rivals",
                   note="each rival's ratings ÷ its age in months, added up (ratings stand in for downloads)"),
        Ingredient("free_chart", "Top-free chart", 1, "num2", post=" chart points", zero="no rival on a top-free chart",
                   note="rivals on an Apple top-free chart in the last 30 days (Apple ranks it by downloads); #1 = 1 point, #10 = 0.57, #100 = 0.13"),
    ]),
    "paying": IndexSpec("idx_paying", "Paying", "Do users of these apps pay?", [
        Ingredient("grossing", "Top-grossing chart", 2, "num2", post=" chart points", zero="no rival on a top-grossing chart",
                   note="rivals on an Apple top-grossing chart in the last 30 days (Apple ranks it by money); #1 = 1 point, #10 = 0.57, #100 = 0.13"),
        Ingredient("lead_price", "Leaders' price", 1, "usd", pre="~", post="/month", zero="the biggest rivals are free",
                   note="median monthly price of the 5 biggest rivals with known prices (one-time purchases ÷ 12)"),
    ]),
    "momentum": IndexSpec("idx_momentum", "Momentum", "Is it growing now?", [
        Ingredient("apple_trend", "Apple search trend", 2, "num1s", post=" search-popularity points per 3 months",
                   note="slope of Apple's weekly search popularity over the last ~6 months (weeks the term was on Apple's list)"),
        Ingredient("google_trend", "Google search trend", 1, "pct1s", post=" Google searches per month",
                   note="growth per month of Google monthly search volume, fitted over the last 6 months"),
        Ingredient("pace", "Rating pace", 1, "times", pre="rivals gain ratings at ", post=" their lifetime pace",
                   note="new ratings per day over our own snapshots vs the app's lifetime average; early until 4 weeks of snapshots"),
        Ingredient("young", "Young rivals", 1, "pct", post=" of rivals are under 1 year old",
                   note="share of the real rivals released in the last 12 months"),
    ], min_coverage=0.4),
    "demand": IndexSpec("idx_demand", "Search demand", "How often do people search for it?", [
        Ingredient("apple_pop", "Apple search popularity", 2, "int", post=" on Apple's 5-100 scale",
                   note="Apple Ads search popularity (relative, not a count)"),
        Ingredient("google_vol", "Google searches", 1, "int", post=" Google searches/month",
                   note="Google Ads monthly search volume, US (web searches, not App Store)"),
    ]),
}


def _apple_trend(con, store: str, snapshot_date: dt.date) -> dict[str, float]:
    """Least-squares slope of weekly popularity, per 13 weeks; needs >= 6 weeks seen over >= 8 weeks."""
    by: dict[str, list[tuple[int, float]]] = {}
    for term, week, pop in con.execute("""SELECT term, week, max(pop_1to100) FROM popularity_weekly WHERE country=? AND week<=?
            AND week > ?::DATE - 190 GROUP BY 1, 2""", [store, snapshot_date, snapshot_date]).fetchall():
        by.setdefault(term, []).append(((week - snapshot_date).days / 7, float(pop or 0)))
    out = {}
    for t, pts in by.items():
        if len(pts) < 6 or max(x for x, _ in pts) - min(x for x, _ in pts) < 8:
            continue
        mx, my = statistics.fmean(x for x, _ in pts), statistics.fmean(y for _, y in pts)
        sxx = sum((x - mx) ** 2 for x, _ in pts)
        out[t] = round(13 * sum((x - mx) * (y - my) for x, y in pts) / sxx, 2) if sxx else 0.0
    return out


def _google(con, snapshot_date: dt.date) -> tuple[dict[str, float], dict[str, float]]:
    vol, trend = {}, {}
    for kw, v, monthly in con.execute("""SELECT keyword, search_volume, monthly FROM keyword_volume WHERE snapshot_date<=?
            QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [snapshot_date]).fetchall():
        if v is None:
            continue
        vol[kw] = float(v)
        m = sorted(json.loads(monthly or "[]"), key=lambda x: (x.get("year", 0), x.get("month", 0)))
        vals = [x.get("search_volume") or 0 for x in m]
        last = vals[-6:]
        if len(last) == 6 and sum(last) >= 60:      # under ~10 searches/month the buckets are noise
            # monthly growth from a log-slope over the last 6 months; "last 6 vs the 6 before" compared summer with
            # winter and disagreed with Apple's trend (rank corr -0.07); the 6-month slope agrees (+0.27)
            mx = 2.5
            ly = [math.log(v + 1) for v in last]
            my = statistics.fmean(ly)
            trend[kw] = round(math.exp(sum((i - mx) * (y - my) for i, y in enumerate(ly)) / 17.5) - 1, 4)
    return vol, trend


def _compute(con, store: str, snapshot_date: dt.date) -> dict[str, dict]:
    key = (id(con), store, snapshot_date)
    if key in _cache:
        return _cache[key]
    from ..sources.appstore_page import period_of
    from .sector import sector_estimates
    est = sector_estimates(con, store, snapshot_date)
    now = dt.datetime.combine(snapshot_date, dt.time())
    ids = list({a.app_id for apps in est.values() for a in apps if a.rank <= TOP})
    iap: dict[str, list[tuple[str, float]]] = {}
    checked: set[str] = set()
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='app_iap_checked'").fetchone()[0]:
        checked = {a for (a,) in con.execute("SELECT DISTINCT app_id FROM app_iap_checked WHERE store=? AND app_id IN (SELECT unnest(?))",
                                             [store, ids]).fetchall()}
        for a, item, price in con.execute("""SELECT app_id, item, price_usd FROM app_iap WHERE store=? AND app_id IN (SELECT unnest(?))
                QUALIFY fetched_date = max(fetched_date) OVER (PARTITION BY app_id)""", [store, ids]).fetchall():
            iap.setdefault(a, []).append((item or "", price or 0.0))
    chart = {c: dict(con.execute("""SELECT app_id, min(rank) FROM chart_entries WHERE store=? AND chart=?
            AND snapshot_date >= ?::DATE - 30 GROUP BY 1""", [store, c, snapshot_date]).fetchall()) for c in ("grossing", "free")}
    genre = dict(con.execute("""SELECT app_id, primary_genre_id FROM apps WHERE store=? AND app_id IN (SELECT unnest(?))
            QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1""", [store, ids]).fetchall())
    hist: dict[str, tuple[float, int]] = {}
    for a, d0, r0, d1, r1 in con.execute("""SELECT app_id, min(snapshot_date), arg_min(rating_count, snapshot_date), max(snapshot_date),
            arg_max(rating_count, snapshot_date) FROM apps WHERE store=? AND app_id IN (SELECT unnest(?)) AND snapshot_date <= ?
            GROUP BY 1 HAVING count(DISTINCT snapshot_date) >= 2""", [store, ids, snapshot_date]).fetchall():
        if r0 is not None and r1 is not None and (d1 - d0).days >= 3:
            hist[a] = (max(0.0, (r1 - r0) / (d1 - d0).days * 30.4), (d1 - d0).days)
    apple_pop = {k: float(v) for k, v in con.execute("""SELECT entity_id, value FROM factor_values WHERE metric='demand' AND entity_type='keyword'
            AND snapshot_date<=? QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""", [snapshot_date]).fetchall()}
    a_trend = _apple_trend(con, store, snapshot_date)
    g_vol, g_trend = _google(con, snapshot_date)

    raw: dict[str, dict] = {}
    cat: dict[str, str] = {}
    extra: dict[str, dict] = {}
    for kw, apps in est.items():
        rivals = [a for a in apps if a.rank <= TOP]
        if not rivals:
            continue
        rpm, accel, young, span, known = [], [], [], [], []
        gw: dict[str, float] = {}
        for a in rivals:
            months = max(1.0, (now - a.release_date).days / 30.4) if a.release_date else None
            m_rate = (a.rating_count or 0) / months if months else 0.0
            rpm.append(m_rate)
            g = GENRES.get(str(genre.get(a.app_id) or ""))
            if g:
                gw[g] = gw.get(g, 0.0) + m_rate + 1
            if a.app_id in checked:
                items = [(i, p) for i, p in iap.get(a.app_id, []) if p and p > 0]
                subs = [p * PER_MONTH[period_of(i, p)] for i, p in items if period_of(i, p) in PER_MONTH]
                once = [p for i, p in items if period_of(i, p) not in PER_MONTH] + ([float(a.price)] if (a.price or 0) > 0 else [])
                known.append((m_rate, max(subs) if subs else (max(once) / 12 if once else 0.0)))
            if a.app_id in hist and m_rate >= 3:
                accel.append(min(10.0, hist[a.app_id][0] / m_rate)); span.append(hist[a.app_id][1])
            young.append(1.0 if months is not None and months <= 12 else 0.0)
        leaders = sorted(known, key=lambda x: -x[0])[:5]
        best_g = min((chart["grossing"][a.app_id] for a in rivals if a.app_id in chart["grossing"]), default=None)
        raw[kw] = {
            "ratings_month": sum(rpm),
            "free_chart": sum(_pts(chart["free"][a.app_id]) for a in rivals if a.app_id in chart["free"]),
            "grossing": sum(_pts(chart["grossing"][a.app_id]) for a in rivals if a.app_id in chart["grossing"]),
            "lead_price": statistics.median(p for _, p in leaders) if leaders else None,
            "apple_trend": a_trend.get(kw), "google_trend": g_trend.get(kw),
            "pace": statistics.median(accel) if accel else None, "young": sum(young) / len(young),
            "apple_pop": apple_pop.get(kw), "google_vol": g_vol.get(kw),
        }
        if gw:
            cat[kw] = max(gw, key=gw.get)
        extra[kw] = {"rivals": len(rivals), "prices_known": len(known), "best_grossing_rank": best_g,
                     "on_grossing": sum(1 for a in rivals if a.app_id in chart["grossing"]),
                     "on_free": sum(1 for a in rivals if a.app_id in chart["free"]), "history_days": max(span) if span else 0}
    for kw in set(apple_pop) - set(raw):     # searched-for terms without a results page still get Search demand
        raw[kw] = {"apple_pop": apple_pop[kw], "google_vol": g_vol.get(kw), "apple_trend": a_trend.get(kw), "google_trend": g_trend.get(kw)}
    out: dict[str, dict] = {}
    for field, spec in SPECS.items():
        for kw, res in build(spec, raw, cat).items():
            res["why"] = why(res)
            res["extra"] = extra.get(kw, {})
            if field == "momentum":
                res["early"] = extra.get(kw, {}).get("history_days", 0) < EARLY_DAYS
            out.setdefault(kw, {})[field] = res
    _cache.clear()
    _cache[key] = out
    return out


class _Score(Metric):
    field: str = ""
    requires = ("apps",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        return [FactorRow(kw, s[self.field]["value"], {k: v for k, v in s[self.field].items() if k != "value"})
                for kw, s in _compute(con, store, snapshot_date).items() if self.field in s]


@metrics.register()
class PopularityIndex(_Score):
    name = "idx_popularity"
    description = "0-100 percentile: rivals' ratings per month (x2) and top-free chart places (x1)."
    field = "popularity"


@metrics.register()
class PayingIndex(_Score):
    name = "idx_paying"
    description = "0-100 percentile: rivals' top-grossing chart places (x2) and the leaders' monthly price (x1)."
    field = "paying"


@metrics.register()
class MomentumIndex(_Score):
    name = "idx_momentum"
    description = "0-100 percentile: Apple and Google search trends, rivals' rating pace, young rivals."
    field = "momentum"


@metrics.register()
class DemandIndex(_Score):
    name = "idx_demand"
    description = "0-100 percentile: Apple search popularity (x2) and Google monthly searches (x1)."
    field = "demand"
