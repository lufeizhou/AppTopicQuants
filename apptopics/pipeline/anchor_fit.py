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
