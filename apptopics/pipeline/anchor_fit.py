"""Calibrate the per-app estimator against verified revenue (TrustMRR, MobileMRR) and Play installs.

Two targets ("y"):
  revenue    monthly USD (TrustMRR last-30-days, MobileMRR MRR): worldwide, all platforms.
  downloads  Google Play installs (lifetime) vs Play ratings: downloads_per_rating on Play,
             plus iOS-vs-Play rating ratio for cross-listed apps to carry it over to iOS.
Model (log space): log10(rev) = c + slope * log10(US iOS ratings per month since release).
We report the error of the current constants and of the fit: median |log10 error| and the share
of anchors within 2x / 3x. Anchors are written to calibration_anchors (source trustmrr|mobilemrr)."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
from pathlib import Path

import numpy as np

from ..config import settings
from ..db import today, upsert
from ..metrics.sector import estimate_app_revenue
from ..sources.itunes import ITunesClient, lookup_to_row
from ..sources.revenue_dirs import CACHE

_ID = re.compile(r"id(\d{6,})")


def collect_anchors(min_usd: float = 50.0) -> list[dict]:
    """Named anchors with an App Store id from the cached directory pulls."""
    out = {}
    p, part = CACHE / "trustmrr_mobile.json", CACHE / "trustmrr_mobile.partial.jsonl"
    tm = json.loads(p.read_text()) if p.exists() else ([json.loads(l) for l in part.read_text().splitlines() if l.strip()] if part.exists() else [])
    if tm:
        for x in tm:
            ma = (x.get("mobileApp") or {})
            url = ((ma.get("appStore") or {}).get("url")) or (x.get("website") if "apps.apple.com" in (x.get("website") or "") else None)
            m = _ID.search(url or "")
            rev = (x.get("revenue") or {}).get("last30Days") or (x.get("revenue") or {}).get("mrr") or 0
            if m and rev >= min_usd and not x.get("stealthMode"):
                gp = (ma.get("googlePlay") or {})
                pk = re.search(r"id=([\w.]+)", gp.get("url") or "")
                out[m.group(1)] = {"app_id": m.group(1), "source": "trustmrr", "name": x.get("name"), "monthly_usd": float(rev),
                                   "play_package": pk.group(1) if pk else None, "play_ratings": gp.get("ratingCount")}
    p = CACHE / "mobilemrr_leaderboard.json"
    if p.exists():
        for x in json.loads(p.read_text()):
            m = _ID.search(x.get("appStoreUrl") or "")
            if m and (x.get("mrr") or 0) >= min_usd and x.get("revenueVerified") and m.group(1) not in out:
                pk = re.search(r"id=([\w.]+)", x.get("playStoreUrl") or "")
                out[m.group(1)] = {"app_id": m.group(1), "source": "mobilemrr", "name": x.get("name"), "monthly_usd": float(x["mrr"]),
                                   "play_package": pk.group(1) if pk else None, "play_ratings": None}
    return list(out.values())


def _err(pred: list[float], real: list[float]) -> dict:
    e = [abs(math.log10(max(p, 1)) - math.log10(max(r, 1))) for p, r in zip(pred, real)]
    bias = statistics.median(math.log10(max(p, 1)) - math.log10(max(r, 1)) for p, r in zip(pred, real))
    return {"n": len(e), "median_abs_log10": round(statistics.median(e), 3), "within_2x": round(sum(x <= math.log10(2) for x in e) / len(e), 2),
            "within_3x": round(sum(x <= math.log10(3) for x in e) / len(e), 2), "median_bias_x": round(10 ** bias, 2)}


def fit(con, store: str = "us") -> dict:
    anchors = collect_anchors()
    if not anchors:
        return {"n": 0, "error": "no anchors cached (run the TrustMRR/MobileMRR pulls)"}
    d = today()
    looked = {str(r["trackId"]): r for r in ITunesClient().lookup([a["app_id"] for a in anchors], store)}
    upsert(con, "apps", [lookup_to_row(r, store, d) for r in looked.values()])
    upsert(con, "calibration_anchors", [{"app_id": a["app_id"], "store": store, "source": a["source"], "monthly_revenue_usd": a["monthly_usd"],
                                         "monthly_downloads": None, "observed_at": d, "note": a["name"]} for a in anchors])
    cfg = settings.weights().get("monetization", {})
    now = dt.datetime.combine(d, dt.time())
    rows = []
    for a in anchors:
        r = looked.get(a["app_id"])
        if not r:
            continue
        row = lookup_to_row(r, store, d)
        rc, rel = row["rating_count"] or 0, row["release_date"]
        months = max(1.0, (now - rel).days / 30.4) if rel else None
        est, src = estimate_app_revenue(cfg, d, rc, row["rating_count_current"], rel, row["current_version_date"], None)
        rows.append({**a, "ratings": rc, "months": months, "rpm": (rc / months) if months else None, "est_now": est, "src": src,
                     "genre": row["primary_genre_id"], "price": row["price"]})
    usable = [r for r in rows if r["rpm"] and r["rpm"] > 0]
    real = [r["monthly_usd"] for r in usable]
    res = {"anchors": len(anchors), "matched_on_store": len(rows), "usable": len(usable),
           "zero_us_ratings": sum(1 for r in rows if not r["ratings"]),
           "current_constants": _err([r["est_now"] for r in usable], real)}
    x = np.log10([r["rpm"] for r in usable]); y = np.log10(real)
    # (a) slope fixed at 1: one $ per rating-per-month constant
    k1 = 10 ** float(np.median(y - x))
    res["fit_slope1"] = {"usd_per_rating_month": round(k1, 1), **_err([k1 * r["rpm"] for r in usable], real)}
    # (b) free slope (OLS)
    slope, c = np.polyfit(x, y, 1)
    pred = 10 ** (c + slope * x)
    r2 = 1 - float(np.sum((y - (c + slope * x)) ** 2) / np.sum((y - y.mean()) ** 2))
    res["fit_free"] = {"slope": round(float(slope), 3), "c": round(float(c), 3), "r2": round(r2, 3), **_err(list(pred), real)}
    res["spearman_rpm_vs_revenue"] = round(float(np.corrcoef(np.argsort(np.argsort(x)), np.argsort(np.argsort(y)))[0, 1]), 3)
    # downloads target: Play installs per Play rating, and iOS ratings vs Play ratings for cross-listed anchors
    play = con.execute("""SELECT max_installs, min_installs, ratings FROM play_apps WHERE ratings > 50
                          QUALIFY row_number() OVER (PARTITION BY package ORDER BY snapshot_date DESC)=1""").fetchall()
    dpr = [(mx or mn) / rt for mx, mn, rt in play if (mx or mn) and rt]
    res["play_downloads_per_rating"] = {"n": len(dpr), "median": round(statistics.median(dpr), 1),
                                        "iqr": [round(float(np.percentile(dpr, q)), 1) for q in (25, 75)]} if dpr else None
    cross = [(r["ratings"], r["play_ratings"]) for r in rows if r.get("play_ratings") and r["ratings"]]
    if cross:
        res["ios_to_play_rating_ratio"] = {"n": len(cross), "median": round(statistics.median(i / p for i, p in cross), 3)}
    res["by_revenue_band"] = {}
    for lo, hi in [(0, 1000), (1000, 5000), (5000, 20000), (20000, 1e9)]:
        sub = [r for r in usable if lo <= r["monthly_usd"] < hi]
        if sub:
            res["by_revenue_band"][f"${lo:,.0f}-{hi:,.0f}"] = {"n": len(sub), "median_ratings": int(statistics.median(r["ratings"] for r in sub)),
                                                             "median_rpm": round(statistics.median(r["rpm"] for r in sub), 1)}
    res["examples"] = sorted([{"name": r["name"], "usd": round(r["monthly_usd"]), "us_ratings": r["ratings"], "rpm": round(r["rpm"], 1),
                               "est_now": round(r["est_now"])} for r in usable], key=lambda z: -z["usd"])[:15]
    return res

def import_anchors(con, path: Path, source: str | None = None) -> int:
    """CSV with columns: app_id, monthly_revenue_usd[, monthly_downloads, source, note, observed_at]."""
    import csv
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"app_id": str(r["app_id"]).strip(), "store": r.get("store") or "us", "source": source or r.get("source") or "manual",
                         "monthly_revenue_usd": float(r["monthly_revenue_usd"]), "monthly_downloads": int(float(r["monthly_downloads"])) if r.get("monthly_downloads") else None,
                         "observed_at": dt.date.fromisoformat(r["observed_at"]) if r.get("observed_at") else today(), "note": r.get("note")})
    return upsert(con, "calibration_anchors", rows)


# ---------------------------------------------------------------------------------------------------------------------
# Honest calibration: reproducible knot fit, a direct P(>= target) model, and hold-out evaluation of both.
# ---------------------------------------------------------------------------------------------------------------------
P5K_FEATURES = ("log_rpm", "log_ratings", "log_months", "paid")


def anchor_rows(con, store: str = "us") -> list[dict]:
    """Verified-revenue anchors joined with their latest App Store profile (no network). One row per app."""
    rows = con.execute("""
        WITH a AS (SELECT app_id, source, monthly_revenue_usd, note FROM calibration_anchors WHERE monthly_revenue_usd > 0
                   QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY observed_at DESC)=1),
             p AS (SELECT app_id, rating_count, rating_count_current, release_date, current_version_date, price, primary_genre_id, snapshot_date
                   FROM apps WHERE store=? QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1)
        SELECT a.app_id, a.source, a.monthly_revenue_usd, a.note, p.rating_count, p.rating_count_current, p.release_date, p.current_version_date,
               p.price, p.primary_genre_id, p.snapshot_date FROM a JOIN p USING (app_id)""", [store]).fetchall()
    out = []
    for app_id, src, usd, name, rc, rcc, rel, ver, price, genre, d in rows:
        if not rc or not rel:
            continue
        months = max(1.0, (dt.datetime.combine(d, dt.time()) - rel).days / 30.4)
        out.append({"app_id": app_id, "source": src, "name": name, "usd": float(usd), "ratings": int(rc), "rcc": rcc, "months": months,
                    "rpm": rc / months, "price": price or 0.0, "genre": genre, "release_date": rel, "version_date": ver, "asof": d})
    return out


def fit_knots(rows: list[dict], grid=(0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256), window: float = 0.4, min_n: int = 30) -> tuple[list, list]:
    """Running median of monthly revenue around each ratings-per-month knot (log window +-`window` decades),
    kept only where >= min_n anchors fall in the window, made monotone. This is how config/weights.toml was built."""
    xs, ys = [], []
    for k in grid:
        sub = [r["usd"] for r in rows if abs(math.log10(r["rpm"]) - math.log10(k)) <= window]
        if len(sub) >= min_n:
            xs.append(k); ys.append(statistics.median(sub))
    for i in range(1, len(ys)):
        ys[i] = max(ys[i], ys[i - 1])
    return xs, [round(y) for y in ys]


def _feat(r: dict) -> list[float]:
    return [1.0, math.log10(r["rpm"] + 0.01), math.log10(r["ratings"] + 1), math.log10(r["months"]), 1.0 if (r["price"] or 0) > 0 else 0.0]


def fit_logistic(rows: list[dict], target: float, l2: float = 1.0, iters: int = 50) -> list[float]:
    """Regularised logistic regression P(usd >= target | features) by Newton's method (no sklearn needed)."""
    X = np.array([_feat(r) for r in rows]); y = np.array([1.0 if r["usd"] >= target else 0.0 for r in rows])
    w = np.zeros(X.shape[1]); reg = l2 * np.eye(X.shape[1]); reg[0, 0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + reg @ w
        H = (X * (p * (1 - p))[:, None]).T @ X + reg
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return [round(float(v), 4) for v in w]


def p_logistic(coef: list[float], r: dict) -> float:
    z = sum(c * x for c, x in zip(coef, _feat(r)))
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def _auc(p: list[float], y: list[int]) -> float | None:
    pos = [a for a, b in zip(p, y) if b]; neg = [a for a, b in zip(p, y) if not b]
    if not pos or not neg:
        return None
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def _hit_table(p: list[float], y: list[int], cut: float = 0.5) -> dict:
    tp = sum(1 for a, b in zip(p, y) if a >= cut and b); fp = sum(1 for a, b in zip(p, y) if a >= cut and not b)
    fn = sum(1 for a, b in zip(p, y) if a < cut and b)
    return {"flagged": tp + fp, "hits": tp, "recall": round(tp / max(1, tp + fn), 2), "precision": round(tp / max(1, tp + fp), 2)}


def _buckets(p: list[float], y: list[int]) -> list[dict]:
    out = []
    for lo, hi in ((0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)):
        sub = [(a, b) for a, b in zip(p, y) if lo <= a < hi]
        if sub:
            out.append({"predicted": f"{lo:.0%}-{min(hi, 1):.0%}", "n": len(sub), "mean_p": round(sum(a for a, _ in sub) / len(sub), 2),
                        "actual": round(sum(b for _, b in sub) / len(sub), 2)})
    return out


def evaluate(rows: list[dict], knots: tuple[list, list], sigma: float, coef: list[float] | None, target: float) -> dict:
    """Curve (point estimate + lognormal P) and logistic P on the same rows."""
    from ..metrics.sector import p_above, rpm_to_usd
    cfg = {"rpm_knots": knots[0], "usd_knots": knots[1], "est_sigma_log10": sigma}
    est = [rpm_to_usd(cfg, r["rpm"]) for r in rows]; real = [r["usd"] for r in rows]; y = [1 if r["usd"] >= target else 0 for r in rows]
    p_curve = [p_above(cfg, e, target) for e in est]
    out = {"n": len(rows), "positives": sum(y), "curve": {**_err(est, real), "point_at_target": _hit_table([1.0 if e >= target else 0.0 for e in est], y),
                                                           "p_at_50": _hit_table(p_curve, y), "auc": _auc(p_curve, y),
                                                           "brier": round(float(np.mean([(a - b) ** 2 for a, b in zip(p_curve, y)])), 3),
                                                           "calibration": _buckets(p_curve, y)}}
    if coef:
        p = [p_logistic(coef, r) for r in rows]
        out["logistic"] = {"p_at_50": _hit_table(p, y), "p_at_35": _hit_table(p, y, 0.35), "auc": _auc(p, y),
                           "brier": round(float(np.mean([(a - b) ** 2 for a, b in zip(p, y)])), 3), "calibration": _buckets(p, y)}
    return out


def holdout(rows: list[dict], target: float, splits: int = 20, test_share: float = 1 / 3, seed: int = 7) -> dict:
    """Refit knots, sigma and the logistic model on 2/3 of the anchors, score the other 1/3; averaged over random splits."""
    rng = np.random.default_rng(seed)
    acc: dict[str, list[float]] = {}
    for _ in range(splits):
        idx = rng.permutation(len(rows)); k = int(len(rows) * test_share)
        test, train = [rows[i] for i in idx[:k]], [rows[i] for i in idx[k:]]
        knots = fit_knots(train)
        if len(knots[0]) < 3:
            continue
        from ..metrics.sector import rpm_to_usd
        cfgk = {"rpm_knots": knots[0], "usd_knots": knots[1]}
        sigma = float(np.std([math.log10(max(rpm_to_usd(cfgk, r["rpm"]), 1)) - math.log10(r["usd"]) for r in train]))
        ev = evaluate(test, knots, sigma, fit_logistic(train, target), target)
        for name, m in (("curve_point", ev["curve"]["point_at_target"]), ("curve_p50", ev["curve"]["p_at_50"]),
                        ("logistic_p50", ev["logistic"]["p_at_50"]), ("logistic_p35", ev["logistic"]["p_at_35"])):
            for kk in ("recall", "precision"):
                acc.setdefault(f"{name}.{kk}", []).append(m[kk])
        acc.setdefault("curve.within_3x", []).append(ev["curve"]["within_3x"])
        acc.setdefault("curve.median_abs_log10", []).append(ev["curve"]["median_abs_log10"])
        for name in ("curve", "logistic"):
            if ev[name].get("auc") is not None:
                acc.setdefault(f"{name}.auc", []).append(ev[name]["auc"])
            acc.setdefault(f"{name}.brier", []).append(ev[name]["brier"])
    return {k: round(float(np.mean(v)), 3) for k, v in sorted(acc.items())} | {"splits": splits, "test_share": round(test_share, 2)}


def calibrate(con, store: str = "us", apply: bool = False) -> dict:
    """The honest report: in-sample fit of the curve and of a direct logistic P(>= target), hold-out scores of both,
    and (with apply) the refitted curve written to weights.toml. The logistic model is reported, not used: on hold-out it
    ranks no better than the curve (AUC 0.74 vs 0.73 on 2026-11-04) and its probabilities are flatter."""
    from ..metrics.sector import rpm_to_usd
    target = float(settings.weights().get("target", {}).get("monthly_usd", 5000))
    rows = anchor_rows(con, store)
    if len(rows) < 60:
        return {"n": len(rows), "error": "too few anchors with a store profile (run calibrate-anchors first)"}
    knots = fit_knots(rows)
    cfgk = {"rpm_knots": knots[0], "usd_knots": knots[1]}
    sigma = round(float(np.std([math.log10(max(rpm_to_usd(cfgk, r["rpm"]), 1)) - math.log10(r["usd"]) for r in rows])), 3)
    coef = fit_logistic(rows, target)
    cur = settings.weights().get("monetization", {})
    res = {"anchors": len(rows), "positives": sum(1 for r in rows if r["usd"] >= target), "target": target,
           "current": {"knots": [cur.get("rpm_knots"), cur.get("usd_knots")], "sigma": cur.get("est_sigma_log10"),
                       "in_sample": evaluate(rows, (cur.get("rpm_knots") or knots[0], cur.get("usd_knots") or knots[1]), float(cur.get("est_sigma_log10", 0.77)), None, target)},
           "refit": {"knots": knots, "sigma": sigma, "p5k_coef": dict(zip(("intercept", *P5K_FEATURES), coef)),
                     "in_sample": evaluate(rows, knots, sigma, coef, target)},
           "holdout": holdout(rows, target)}
    if apply:
        res["applied"] = apply_calibration(knots, sigma)
    return res


def apply_calibration(knots: tuple[list, list], sigma: float, path: Path | None = None) -> list[str]:
    """Write the refitted curve into config/weights.toml [monetization]; returns the lines written."""
    p = path or settings.weights_path
    text = p.read_text(); changed = []
    for key, val in (("rpm_knots", knots[0]), ("usd_knots", knots[1]), ("est_sigma_log10", sigma)):
        new = f"{key} = {json.dumps(val)}"
        text, n = re.subn(rf"^{key} = .*$", new, text, flags=re.M)
        if n:
            changed.append(new)
    text, n = re.subn(r"^# Calibrated \d{4}-\d{2}-\d{2} on \d+ ", f"# Calibrated {today()} on {{n}} ", text, flags=re.M)
    p.write_text(text)
    return changed
