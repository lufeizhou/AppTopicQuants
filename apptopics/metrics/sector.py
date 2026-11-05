"""Sector economics for a small team.

A keyword's *sector* is its full App Store results page (up to 100 apps). Every app
gets an estimated monthly revenue (see estimate_app_revenue), then the sector is
sorted by that estimate and we ask the questions that matter when you are not
going to be #1:

  breadth        share of sector apps earning >= target          (do 5-20% of them make money?)
  attainable     median est. revenue inside the p5-p20 band vs target
  concentration  share of sector revenue held by the top 5% apps (winner-take-all => avoid)
  entrant_share  share of band apps released in the last 24 months (newcomers reach the band)
  indie_share    share of band apps from small developers
  monetization   log10 total est. sector revenue (someone pays here at all)

Per-app estimate, in order of preference, all UNCALIBRATED until anchors are fitted:
  1. grossing chart rank -> power-law curve
  2. observed rating velocity: lifetime ratings now minus lifetime ratings in our oldest snapshot
     at least 7 days older (true day-over-day data from the daily light run)
  3. current-version velocity, only when Apple's per-version count is clearly a reset count
     (< 50% of lifetime) and the version is >= 14 days old. (Apple usually reports the lifetime
     count here, which would make a freshly updated app look like it gains thousands of ratings a day.)
  4. lifetime rating velocity: lifetime ratings / months since release
  velocity -> USD via the calibrated ratings-per-month curve (rpm_knots / usd_knots).
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass

from ..config import settings
from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot
from .monetization import rank_to_revenue
from .relevance import patterns, relevance, threshold


@dataclass
class AppEst:
    app_id: str
    rank: int
    name: str
    developer: str
    rating_count: int
    release_date: dt.datetime | None
    price: float | None
    est: float
    src: str
    relevant: bool = True


def rpm_to_usd(cfg: dict, rpm: float) -> float:
    """Calibrated curve: US iOS ratings per month -> median monthly revenue (log-log interpolation)."""
    xs, ys = cfg.get("rpm_knots"), cfg.get("usd_knots")
    if not xs:
        raise ValueError("config/model.toml [monetization] needs rpm_knots / usd_knots (run calibrate-anchors)")
    if rpm <= 0:
        return 0.0
    lx = math.log10(rpm)
    lxs, lys = [math.log10(v) for v in xs], [math.log10(v) for v in ys]
    if lx <= lxs[0]:
        return 10 ** (lys[0] + (lx - lxs[0]))          # below the data: proportional
    if lx >= lxs[-1]:
        return 10 ** (lys[-1] + 0.9 * (lx - lxs[-1]))
    for i in range(1, len(lxs)):
        if lx <= lxs[i]:
            t = (lx - lxs[i - 1]) / (lxs[i] - lxs[i - 1])
            return 10 ** (lys[i - 1] + t * (lys[i] - lys[i - 1]))
    return 10 ** lys[-1]


def p_above(cfg: dict, est: float, target: float) -> float:
    """Probability the app really earns >= target given its estimate (lognormal spread from calibration)."""
    sig = float(cfg.get("est_sigma_log10", 0.77))
    if est <= 0:
        return 0.0
    z = (math.log10(est) - math.log10(target)) / sig
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def estimate_app_revenue(cfg: dict, asof: dt.date, rating_count: int | None, rc_current: int | None,
                         release_date: dt.datetime | None, version_date: dt.datetime | None,
                         best_eff_rank: int | None, observed: tuple[int, int] | None = None) -> tuple[float, str]:
    """observed = (ratings gained, days elapsed) from our own snapshots, when available."""
    if best_eff_rank is not None:
        return rank_to_revenue(best_eff_rank, float(cfg.get("a", 1.5e6)), float(cfg.get("b", 1.1))), "grossing"
    now = dt.datetime.combine(asof, dt.time())
    if observed and observed[1] >= 7 and observed[0] >= 3:     # 0-2 ratings in a week is noise: fall back to the lifetime rate
        return rpm_to_usd(cfg, observed[0] / observed[1] * 30.4), "observed"
    if (version_date and rc_current and rating_count and (now - version_date).days >= 14
            and rc_current < 0.5 * rating_count):
        return rpm_to_usd(cfg, rc_current / (now - version_date).days * 30.4), "velocity"
    if release_date and rating_count:
        months = max(1.0, (now - release_date).days / 30.4)
        return rpm_to_usd(cfg, rating_count / months), "lifetime"
    return float(rating_count or 0) * float(cfg.get("usd_per_rating_month", 0.5)), "ratings"


def genre_curve_levels(con, store: str, cd, asof: dt.date, cfg: dict) -> dict[str, float]:
    """Per-chart level a_g of revenue = a_g * rank^-b. Genres differ ~100x (rank 100 in Productivity ~ $32k/mo,
    in Travel ~ $1.5k), so 'genre rank x 8 = overall rank' was wrong. a_g = median over the chart's apps of
    (anchor-calibrated rating estimate * rank^b): the charts give the ordering, the anchors give the dollars."""
    b = float(cfg.get("b", 0.86))
    now = dt.datetime.combine(asof, dt.time())
    rows = con.execute("""
        WITH a AS (SELECT app_id, rating_count, release_date FROM apps WHERE store=?
                   QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1)
        SELECT coalesce(nullif(c.genre_id,''),'all'), c.rank, a.rating_count, a.release_date
        FROM chart_entries c JOIN a USING (app_id)
        WHERE c.store=? AND c.chart='grossing' AND c.snapshot_date=? AND a.rating_count>0 AND a.release_date IS NOT NULL""",
                       [store, store, cd]).fetchall()
    by: dict[str, list[float]] = {}
    for g, rank, rc, rel in rows:
        by.setdefault(g, []).append(rpm_to_usd(cfg, rc / max(1.0, (now - rel).days / 30.4)) * rank ** b)
    return {g: statistics.median(v) for g, v in by.items() if len(v) >= 20}


def genre_chart_revenue(con, store: str, cd, asof: dt.date, cfg: dict) -> dict[str, float]:
    """app_id -> revenue from its best grossing-chart position (max over charts it appears in)."""
    global _levels
    _levels = genre_curve_levels(con, store, cd, asof, cfg)
    b = float(cfg.get("b", 0.86))
    out: dict[str, float] = {}
    for app_id, g, rank in con.execute("SELECT app_id, coalesce(nullif(genre_id,''),'all'), rank FROM chart_entries "
                                       "WHERE store=? AND chart='grossing' AND snapshot_date=?", [store, cd]).fetchall():
        a = _levels.get(g)
        if a:
            out[app_id] = max(out.get(app_id, 0.0), a * rank ** -b)
    return out


_levels: dict[str, float] = {}
_cache: dict[tuple, dict[str, list[AppEst]]] = {}
_full: dict[str, list[AppEst]] = {}


def sector_estimates(con, store: str, snapshot_date: dt.date) -> dict[str, list[AppEst]]:
    """keyword -> apps sorted by estimated revenue desc. Cached per (store, date)."""
    key = (id(con), store, snapshot_date)
    if key in _cache:
        return _cache[key]
    sd = latest_snapshot(con, "search_results", "store", store, snapshot_date)
    if sd is None:
        return {}
    cd = latest_snapshot(con, "chart_entries", "store", store, snapshot_date)
    cfg = settings.weights().get("monetization", {})
    chart_rev = genre_chart_revenue(con, store, cd, snapshot_date, cfg) if cd is not None else {}
    rows = con.execute("""
        WITH a AS (SELECT app_id, release_date, current_version_date, rating_count_current, price, rating_count, snapshot_date FROM apps
                   WHERE store=? AND snapshot_date<=? QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1),
             o AS (SELECT app_id, rating_count AS rc_old, snapshot_date AS d_old FROM apps
                   WHERE store=? AND snapshot_date <= ? - INTERVAL 7 DAY QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date)=1)
        SELECT s.keyword, s.rank, s.app_id, s.name, s.developer, coalesce(a.rating_count, s.rating_count),
               a.release_date, a.current_version_date, a.rating_count_current, a.price, o.rc_old, o.d_old, a.snapshot_date
        FROM sr_latest s LEFT JOIN a USING (app_id) LEFT JOIN o USING (app_id)
        WHERE s.store=? AND s.snapshot_date=? ORDER BY s.keyword, s.rank""", [store, snapshot_date, store, snapshot_date, store, sd]).fetchall()
    use_rel = settings.weights().get("filters", {}).get("relevant_apps_only", True)
    relmap = relevance(con, store, sd) if use_rel else {}
    thr = {}
    full: dict[str, list[AppEst]] = {}
    for kw, rank, app_id, name, dev, rc, rel, ver, rcc, price, rc_old, d_old, d_new in rows:
        observed = (int(rc) - int(rc_old), (d_new - d_old).days) if rc_old is not None and rc is not None and d_new and d_old else None
        if app_id in chart_rev:
            est, src = chart_rev[app_id], "grossing"
        else:
            est, src = estimate_app_revenue(cfg, snapshot_date, rc, rcc, rel, ver, None, observed)
        if kw not in thr:
            thr[kw] = threshold(len(patterns(kw)))
        ok = (not use_rel) or relmap.get((kw, app_id), 1.0) >= thr[kw]
        full.setdefault(kw, []).append(AppEst(app_id, rank, name, dev or "", rc or 0, rel, price, est, src, ok))
    global _full
    _full = full   # every result incl. irrelevant ones (for the intent metric)
    # a keyword's sector = only the apps that actually serve it
    out = {kw: sorted((a for a in apps if a.relevant), key=lambda a: -a.est) for kw, apps in full.items()}
    _cache.clear()
    _cache[key] = out
    return out


def band(apps: list[AppEst], lo: float, hi: float) -> list[AppEst]:
    n = len(apps)
    i0, i1 = math.floor(lo * n), max(math.floor(lo * n) + 1, math.ceil(hi * n))
    return apps[i0:i1]


def _target_cfg():
    t = settings.weights().get("target", {})
    return float(t.get("monthly_usd", 5000)), tuple(t.get("sector_band", [0.05, 0.20])), int(t.get("min_sector_size", 10))


class _SectorMetric(Metric):
    requires = ("search_results",)

    def compute(self, con, store, snapshot_date):
        target, (lo, hi), min_n = _target_cfg()
        out = []
        for kw, apps in sector_estimates(con, store, snapshot_date).items():
            if len(apps) < min_n:
                continue
            row = self.one(kw, apps, band(apps, lo, hi), target)
            if row is not None:
                out.append(row)
        return out

    def one(self, kw, apps, b, target):  # pragma: no cover - abstract
        raise NotImplementedError


@metrics.register()
class Breadth(_SectorMetric):
    name = "breadth"
    description = "Expected share of sector apps earning >= target (mean calibrated probability per app)."
    higher_is_better = True

    def one(self, kw, apps, b, target):
        cfg = settings.weights().get("monetization", {})
        n_above = sum(1 for a in apps if a.est >= target)
        share = sum(p_above(cfg, a.est, target) for a in apps) / len(apps)
        return FactorRow(kw, round(share, 3), {"n_above_target_point": n_above, "n": len(apps),
                         "expected_n_above": round(share * len(apps), 1),
                         "src_mix": {s: sum(1 for a in apps if a.src == s) for s in ("grossing", "observed", "velocity", "lifetime", "ratings")}})


@metrics.register()
class Attainable(_SectorMetric):
    name = "attainable"
    description = "log10(median est. revenue of the p5-p20 band / target). 0 = band earns exactly the target."
    higher_is_better = True

    def one(self, kw, apps, b, target):
        med = statistics.median(a.est for a in b)
        val = math.log10((med + 1) / target)
        return FactorRow(kw, round(val, 4), {"attainable_usd": round(med), "target_usd": target, "band_n": len(b),
                         "band_examples": [a.name for a in b[:3]], "p_target": round(1 / (1 + math.exp(-2.5 * val)), 3),
                         "calibrated": bool(settings.weights().get("monetization", {}).get("calibrated", False))})


@metrics.register()
class Concentration(_SectorMetric):
    name = "concentration"
    description = "Share of est. sector revenue held by the top 5% of apps (1 = winner takes all)."
    higher_is_better = False

    def one(self, kw, apps, b, target):
        total = sum(a.est for a in apps)
        if total <= 0:
            return None
        k = max(1, math.ceil(0.05 * len(apps)))
        top = sum(a.est for a in apps[:k]) / total
        hhi = sum((a.est / total) ** 2 for a in apps)
        return FactorRow(kw, round(top, 3), {"top_k": k, "hhi": round(hhi, 3), "leader": apps[0].name,
                                             "leader_share": round(apps[0].est / total, 3)})


@metrics.register()
class EntrantShare(_SectorMetric):
    name = "entrant_share"
    description = "Share of p5-p20 band apps released in the last 24 months: newcomers do reach the paying band."
    requires = ("search_results", "apps")
    higher_is_better = True

    def one(self, kw, apps, b, target):
        asof = dt.datetime.now()
        dated = [a for a in b if a.release_date is not None]
        if not dated:
            return None
        new = [a for a in dated if (asof - a.release_date).days <= 730]
        return FactorRow(kw, round(len(new) / len(dated), 3), {"n_dated": len(dated), "new": [a.name for a in new[:5]]})


@metrics.register()
class NewcomerWinners(_SectorMetric):
    name = "newcomer_winners"
    description = ("Young apps already winning here: apps released in the last 24 months estimated at >= target anywhere in the "
                   "sector (not only the p5-p20 band), plus half credit for those within x2 of it. Stored as log(1 + count).")
    requires = ("search_results", "apps")
    higher_is_better = True

    def one(self, kw, apps, b, target):
        cfg = settings.weights().get("monetization", {})
        asof = dt.datetime.now()
        dated = [a for a in apps if a.release_date is not None]
        if not dated:
            return None
        young = [a for a in dated if (asof - a.release_date).days <= 730]
        point = sorted((a for a in young if a.est >= target), key=lambda a: -a.est)
        near = [a for a in young if target / 2 <= a.est < target]        # within the x2 error of the estimate: half credit
        count = len(point) + 0.5 * len(near)
        return FactorRow(kw, round(math.log1p(count), 4), {
            "count": count, "n_point": len(point), "n_near": len(near), "n_young": len(young), "n_dated": len(dated),
            "expected_prob": round(sum(p_above(cfg, a.est, target) for a in young), 2),
            "winners": [f"{a.name} (${a.est:,.0f}/mo, {(asof - a.release_date).days // 30} months)" for a in point[:5]]})


@metrics.register()
class IndieShare(_SectorMetric):
    name = "indie_share"
    description = "Share of p5-p20 band apps from small developers (<=2 apps in sector, <100k ratings in sector)."
    higher_is_better = True

    def one(self, kw, apps, b, target):
        devs: dict[str, list[int]] = {}
        for a in apps:
            devs.setdefault(a.developer, []).append(a.rating_count)
        indie = [a for a in b if len(devs[a.developer]) <= 2 and sum(devs[a.developer]) < 100_000]
        return FactorRow(kw, round(len(indie) / len(b), 3), {"indie": [a.name for a in indie[:5]],
                         "paid_share_band": round(sum(1 for a in b if (a.price or 0) > 0) / len(b), 2)})


@metrics.register()
class Intent(Metric):
    name = "intent"
    description = "Share of Apple's top-10 results that actually serve the keyword. Low = brand/vague search Apple answers with unrelated apps."
    requires = ("search_results",)
    higher_is_better = True

    def compute(self, con, store, snapshot_date):
        sector_estimates(con, store, snapshot_date)
        out = []
        for kw, apps in _full.items():
            top = sorted(apps, key=lambda a: a.rank)[:10]
            if top:
                n_rel = sum(1 for a in apps if a.relevant)
                out.append(FactorRow(kw, round(sum(a.relevant for a in top) / len(top), 2),
                                     {"relevant_apps": n_rel, "results": len(apps),
                                      "unrelated_top": [a.name for a in top if not a.relevant][:4]}))
        return out
