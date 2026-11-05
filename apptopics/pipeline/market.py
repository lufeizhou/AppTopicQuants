"""Market report for a research run: per-aspect crowdedness, competitor profiles, prices, feature map,
what happy users say, and positioning options ("your highlights").

Quant parts come from our own data (search pages, calibrated revenue estimates, ratings, release dates, App Store
in-app purchase prices); descriptive parts from DeepSeek reading competitor descriptions and 4-5 star reviews.

Crowdedness of an aspect (apps that really serve its searches, top 25 of each search):
  Empty      fewer than 3 such apps with >= 50 ratings: little to copy, users must be taught the idea
  Emerging   at most 11 established apps (>= 1,000 ratings): room for a newcomer      <- the sweet spot
  Crowded    12+ established apps
  Saturated  3+ giants (>= 50,000 ratings) and the top 10 of its searches has < 1 weak spot on average
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import statistics
from collections.abc import Callable

from ..config import settings
from ..db import connect_read_live, today, upsert
from .discover import write_retry
from .index import searches_per_day

log = logging.getLogger("apptopics.market")

ESTABLISHED, GIANT, MIN_REAL = 1000, 50000, 50


def crowd_band(n_real: int, established: int, giants: int, avg_weak: float | None) -> tuple[str, int, str]:
    """(band, index 0-100, plain verdict)."""
    idx = round(100 * min(1.0, 0.6 * math.log10(1 + established) / math.log10(31) + 0.4 * math.log10(1 + giants) / math.log10(11)))
    if n_real < 3:
        return "Empty", idx, "Almost no similar apps: you would have to teach people the idea, and few search for it."
    if giants >= 3 and (avg_weak is None or avg_weak < 1):
        return "Saturated", idx, "Several giants and no weak spots on page one: a newcomer will struggle to be seen."
    if established >= 12:
        return "Crowded", idx, "Many established apps: enter only with a sharp angle or a long-tail search."
    return "Emerging", idx, "Some proven apps but room left: the sweet spot for a newcomer."


def _round99(v: float) -> float:
    return max(0.99, math.floor(v) + 0.99) if v >= 1 else round(v, 2)


def price_summary(per_app: dict[str, dict[str, list[float]]], n_apps: int) -> dict:
    """per_app: app_id -> period -> prices. Medians use each app's median price per period."""
    out: dict = {"apps_with_prices": len(per_app), "apps_profiled": n_apps, "periods": {}}
    for per in ("week", "month", "year", "lifetime"):
        vals = sorted(statistics.median(p[per]) for p in per_app.values() if p.get(per))
        if vals:
            q = lambda f: vals[min(len(vals) - 1, int(f * (len(vals) - 1) + 0.5))]
            out["periods"][per] = {"n": len(vals), "share": round(len(vals) / max(1, len(per_app)), 2),
                                   "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "min": vals[0], "max": vals[-1]}
    P = out["periods"]
    sug, why = {}, []
    if "month" in P:
        sug["monthly"] = _round99(P["month"]["median"])
        why.append(f"monthly at the competitors' median (${P['month']['median']:.2f}, {P['month']['n']} apps)")
    if "year" in P:
        sug["yearly"] = _round99(P["year"]["median"])
        why.append(f"yearly at their median (${P['year']['median']:.2f})")
    elif "monthly" in sug:
        sug["yearly"] = _round99(sug["monthly"] * 5)
        why.append("yearly at about 5 months, the usual discount")
    if "week" in P and P["week"]["share"] >= 0.3:
        sug["weekly"] = _round99(P["week"]["median"])
        why.append(f"{round(P['week']['share'] * 100)}% of apps sell a weekly plan, so offer one too")
    if sug.get("monthly") and sug.get("yearly"):
        sug["yearly_discount"] = round(1 - sug["yearly"] / (12 * sug["monthly"]), 2)
    sug["reason"] = "; ".join(why) or "not enough price data; use the category default ($4.99/month, $29.99-39.99/year)"
    out["suggested"] = sug
    return out


def build_market(topic: str, aspects: list[dict], term_aspect: dict[str, str], app: str | None, store: str = settings.store,
                 step: Callable[[str, int], None] | None = None, profile_n: int = 30, review_n: int = 15) -> dict:
    from ..metrics.sector import sector_estimates
    from ..sources.appstore_page import fetch_iap, period_of
    from ..sources.deepseek import DeepSeekClient
    from ..sources.itunes import ITunesClient
    step = step or (lambda *_: None)
    names = [a["name"] for a in aspects] or [topic]
    by_aspect: dict[str, list[str]] = {n: [] for n in names}
    for t, a in term_aspect.items():
        by_aspect.setdefault(a if a in by_aspect else names[0], []).append(t)
    terms = list(term_aspect)
    con = connect_read_live()
    try:
        sec = sector_estimates(con, store, today())
        F: dict[str, dict] = {}
        for e, m, v, mt in con.execute("""SELECT entity_id, metric, value, meta FROM factor_values WHERE entity_type='keyword'
                AND metric IN ('demand','weak_spots','opp_score','attainable','difficulty','buildability') AND entity_id IN (SELECT unnest(?))
                QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1""", [terms]).fetchall():
            F.setdefault(e, {})[m] = (v, json.loads(mt) if mt else {})
        ids = {a.app_id for t in terms for a in sec.get(t, [])}
        info = {r[0]: r[1:] for r in con.execute("""SELECT app_id, name, developer, rating_count, avg_rating, release_date, current_version_date,
                price, description FROM apps WHERE store=? AND app_id IN (SELECT unnest(?))
                QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1""", [store, list(ids)]).fetchall()}
        genre = dict(con.execute("""SELECT app_id, any_value(genre) FROM search_results WHERE store=? AND app_id IN (SELECT unnest(?)) GROUP BY 1""",
                                 [store, list(ids)]).fetchall())
        fresh = {a for (a,) in con.execute("SELECT DISTINCT app_id FROM app_iap_checked WHERE store=? AND fetched_date >= ?",
                                           [store, today() - dt.timedelta(days=14)]).fetchall()}
    finally:
        con.close()
    now = dt.datetime.now()

    def aspects_and_apps(exclude: set[str]) -> tuple[list[dict], dict[str, dict]]:
        apps: dict[str, dict] = {}
        rows = []
        for asp in names:
            ts = by_aspect.get(asp, [])
            seen: dict[str, dict] = {}
            weak = []
            for t in ts:
                for a in sec.get(t, []):
                    if a.rank > 25 or a.app_id in exclude:
                        continue
                    s = seen.setdefault(a.app_id, {"app_id": a.app_id, "name": a.name, "est": a.est, "rc": a.rating_count, "best_rank": a.rank, "n_terms": 0})
                    s["n_terms"] += 1
                    s["best_rank"] = min(s["best_rank"], a.rank)
                w = F.get(t, {}).get("weak_spots")
                if w and (F.get(t, {}).get("demand", (0,))[0] or 0) >= 15:
                    weak.append(w[0])
            real = [s for s in seen.values() if s["rc"] >= MIN_REAL]
            est_ = sum(1 for s in seen.values() if s["rc"] >= ESTABLISHED)
            giants = sum(1 for s in seen.values() if s["rc"] >= GIANT)
            avg_weak = round(sum(weak) / len(weak), 1) if weak else None
            band, idx, verdict = crowd_band(len(real), est_, giants, avg_weak)
            earners = [s for s in seen.values() if s["est"] >= 1000]
            newc = [s for s in earners if (info.get(s["app_id"]) or [None] * 5)[4] and (now - info[s["app_id"]][4]).days <= 730]
            pops = [F[t]["demand"][0] for t in ts if F.get(t, {}).get("demand") and F[t]["demand"][0]]
            best = max((t for t in ts if F.get(t, {}).get("opp_score")), key=lambda t: F[t]["opp_score"][0] or 0, default=None)
            bands = sorted(F[t]["attainable"][1].get("attainable_usd") for t in ts if F.get(t, {}).get("attainable") and F[t]["attainable"][1].get("attainable_usd"))
            top = sorted(seen.values(), key=lambda s: -s["est"])[:8]
            rows.append({"aspect": asp, "n_searches": len(ts), "n_scored": len(pops), "searches_day": round(sum(searches_per_day(p) for p in pops)),
                         "band": band, "crowd_index": idx, "verdict": verdict, "apps_real": len(real), "established": est_, "giants": giants,
                         "avg_weak_spots": avg_weak, "newcomer_share": round(len(newc) / len(earners), 2) if earners else None,
                         "earners_1k": len(earners), "small_apps_earn": bands[len(bands) // 2] if bands else None,
                         "best_search": best, "best_grade": F[best]["opp_score"][1].get("grade") if best else None,
                         "best_score": round(F[best]["opp_score"][0]) if best else None,
                         "build": max((F[t]["buildability"][0] for t in ts if F.get(t, {}).get("buildability")), default=None),
                         "n_a": sum(1 for t in ts if F.get(t, {}).get("opp_score") and F[t]["opp_score"][1].get("grade") == "A"),
                         "top_apps": [{"app_id": s["app_id"], "name": s["name"], "est": round(s["est"]), "ratings": s["rc"]} for s in top]})
            for s in seen.values():
                g = apps.setdefault(s["app_id"], {**s, "aspects": []})
                g["aspects"].append(asp)
                g["est"] = max(g["est"], s["est"])
                g["best_rank"] = min(g["best_rank"], s["best_rank"])
        for a in apps.values():
            _name, dev, rc, avg, rel, ver, price, desc = (info.get(a["app_id"]) or (a["name"], None, a["rc"], None, None, None, None, ""))
            a.update({"developer": dev, "ratings": rc or a["rc"], "stars": round(avg, 2) if avg else None,
                      "released": rel.date().isoformat() if rel else None, "updated": ver.date().isoformat() if ver else None,
                      "age_months": round((now - rel).days / 30.4) if rel else None,
                      "stale": bool(ver and (now - ver).days > 365), "upfront_price": price, "genre": genre.get(a["app_id"]),
                      "_desc": desc or ""})
        return rows, apps

    def ranked_profiles(apps: dict[str, dict], n: int) -> list[dict]:
        ranked = sorted(apps.values(), key=lambda a: -(a["est"] * (1 + 0.3 * (len(a["aspects"]) - 1))))
        return [a for a in ranked if a["ratings"] >= 20][:n]

    rows, apps = aspects_and_apps(set())
    cand = ranked_profiles(apps, profile_n + 10)
    # feature map first: it also tells which apps only share a word with the topic (dropped everywhere below)
    ds = DeepSeekClient()
    features: list[dict] = []
    off_topic: list[str] = []
    if ds.available and cand:
        step("feature map (DeepSeek)", 0)
        try:
            features = ds.feature_taxonomy(topic, [{"id": a["app_id"], "name": a["name"], "text": a["_desc"][:900]} for a in cand[:profile_n]])
            fids = {f["id"] for f in features}
            for i in range(0, len(cand), 10):
                chunk = cand[i:i + 10]
                res = ds.feature_matrix(features, [{"id": a["app_id"], "name": a["name"], "text": a["_desc"][:1600]} for a in chunk], topic, names)
                for a in chunk:
                    r = res.get(a["app_id"]) or {}
                    a["features"] = [f for f in r.get("features", []) if f in fids]
                    a["what"], a["model"] = r.get("what"), r.get("model")
                    if r.get("relevant") is False:
                        off_topic.append(a["app_id"])
                step("feature map (DeepSeek)", int(100 * (i + 10) / len(cand)))
        except Exception as e:
            log.warning("feature map failed: %s", e)
    if off_topic:
        mapped = {a["app_id"]: a for a in cand}
        rows, apps = aspects_and_apps(set(off_topic))
        for aid, a in apps.items():   # keep what the feature map learned
            if aid in mapped:
                a.update({k: mapped[aid].get(k) for k in ("features", "what", "model")})
    prof = ranked_profiles(apps, profile_n)
    # prices
    step("competitor prices (App Store pages)", 0)
    todo = [a["app_id"] for a in prof if a["app_id"] not in fresh]
    got: dict[str, list[dict]] = {}
    for i, aid in enumerate(todo, 1):
        r = fetch_iap(aid, store)
        if r is not None:
            got[aid] = r
        if i % 5 == 0:
            step("competitor prices (App Store pages)", int(100 * i / max(1, len(todo))))
    d = today()
    if got:
        write_retry(lambda c: (upsert(c, "app_iap", [{"store": store, "app_id": aid, "position": x["position"], "item": x["item"], "price_usd": x["price_usd"],
                                                      "period": period_of(x["item"], x["price_usd"]), "fetched_date": d} for aid, xs in got.items() for x in xs]),
                               upsert(c, "app_iap_checked", [{"store": store, "app_id": aid, "fetched_date": d, "n_items": len(xs)} for aid, xs in got.items()])))
    con = connect_read_live()
    try:
        iap: dict[str, list] = {}
        for aid, item, price, per in con.execute("""SELECT app_id, item, price_usd, period FROM app_iap WHERE store=? AND app_id IN (SELECT unnest(?))
                QUALIFY fetched_date = max(fetched_date) OVER (PARTITION BY app_id) ORDER BY app_id, position""", [store, [a["app_id"] for a in prof]]).fetchall():
            iap.setdefault(aid, []).append((item, price, per))
    finally:
        con.close()
    per_app: dict[str, dict[str, list[float]]] = {}
    n_inferred = 0
    for a in prof:
        xs = iap.get(a["app_id"], [])
        tiers: dict[str, list[float]] = {}
        for _item, price, per in xs:
            tiers.setdefault(per, []).append(price)
        # plan names like "Premium" carry no period: for subscription apps with no named period, infer it from the price
        if xs and not any(p in tiers for p in ("week", "month", "year")) and "subscri" in (a.get("model") or "subscription").lower():
            for price in tiers.pop("other", []):
                if price >= 24.99:
                    tiers.setdefault("year", []).append(price)
                elif price >= 4.99:
                    tiers.setdefault("month", []).append(price)
            a["prices_inferred"] = True
            n_inferred += 1
        a["prices"] = {p: sorted(set(v)) for p, v in tiers.items()}
        a["iap_items"] = [f"{i} ${p:.2f}" for i, p, _ in xs][:10]
        if any(p in tiers for p in ("week", "month", "year", "lifetime")):
            per_app[a["app_id"]] = tiers
    prices = price_summary(per_app, len(prof))
    prices["inferred_apps"] = n_inferred
    praise: dict = {}
    if ds.available and prof:
        tot = sum(a["est"] for a in prof) or 1
        for f in features:
            have = [a for a in prof if f["id"] in a.get("features", [])]
            f["coverage"] = round(len(have) / len(prof), 2)
            f["revenue_share"] = round(sum(a["est"] for a in have) / tot, 2)
            f["apps"] = [a["name"] for a in have][:8]
        features.sort(key=lambda f: -f["coverage"])
        step("what happy users say (4-5 star reviews)", 0)
        it, revs = ITunesClient(), []
        for i, a in enumerate(prof[:review_n], 1):
            try:
                rs = [r for r in it.reviews(a["app_id"], store, pages=2) if r["rating"] >= 4 and len(r["body"] or "") >= 40]
            except Exception as e:
                log.warning("reviews %s: %s", a["app_id"], e)
                rs = []
            rs.sort(key=lambda r: -len(r["body"]))
            revs += [{"app": a["name"], "stars": r["rating"], "text": r["body"][:500]} for r in rs[:12]]
            step("what happy users say (4-5 star reviews)", int(100 * i / review_n))
        if revs:
            try:
                praise = ds.praise_mining(topic, revs)
                praise["n_reviews"] = len(revs)
            except Exception as e:
                log.warning("praise mining failed: %s", e)
    for a in prof:
        a.pop("_desc", None)
    comp = [{k: a.get(k) for k in ("app_id", "name", "developer", "what", "model", "aspects", "ratings", "stars", "est", "best_rank", "released",
                                   "updated", "age_months", "stale", "upfront_price", "genre", "prices", "prices_inferred", "iap_items", "features")} for a in prof]
    for c in comp:
        c["est"] = round(c["est"])
    spent = round(ds.cost_usd(), 4) if ds.available else 0
    from .research import _record_deepseek
    _record_deepseek(ds, "market_report", topic)
    return {"aspects": rows, "competitors": comp, "features": features, "prices": prices, "praise": praise,
            "highlights": [], "deepseek_usd": spent, "off_topic": [{"app_id": a, "name": (info.get(a) or ["?"])[0]} for a in off_topic],
            "term_aspect": term_aspect,
            "notes": {"crowd_rule": __doc__.split("Crowdedness")[1].strip(), "revenue": "Revenue = our estimate from ratings growth, "
                      "calibrated on 487 apps with verified revenue (typical error about x2-3 either way)."}}
