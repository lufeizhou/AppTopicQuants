"""DataForSEO supplements (lock-friendly: API calls first, short writes after; spend recorded per job).

  volume_all     Google Ads search volume + CPC + 12-month history for every 'need' keyword    (~$0.09 per 1,000)
  trends_top     Google Trends 5 years, weekly, for the top-N by opportunity; adds seasonality  (~$0.011 per 4 kw)
  play_rankings  Google Play search results per keyword (App Data API) -> play_search          (per task)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics

from .. import budget
from ..config import settings
from ..db import connect_read_live, today, upsert
from ..sources.dataforseo import DataForSEOClient
from .discover import write_retry

log = logging.getLogger("apptopics.dfs")
COMP = {"LOW": 0.2, "MEDIUM": 0.5, "HIGH": 0.85}


def _need_keywords(store: str, order_by_opp: bool = True, limit: int | None = None) -> list[str]:
    con = connect_read_live()
    try:
        d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='opp_score' AND entity_type='keyword'").fetchone()[0]
        rows = con.execute("""
            SELECT l.keyword, coalesce(o.value, 0) FROM keyword_labels l
            LEFT JOIN factor_values o ON o.entity_id=l.keyword AND o.metric='opp_score' AND o.entity_type='keyword' AND o.snapshot_date=?
            WHERE l.store=? AND l.label='need' AND coalesce(l.specificity, 3) >= 2 ORDER BY 2 DESC""", [d, store]).fetchall()
    finally:
        con.close()
    import re
    ok = re.compile(r"^[a-z0-9][a-z0-9 '\-]*$")   # Google Ads rejects symbols ('!', '&', ':', emoji ...)
    kws = [k for k, _ in rows if ok.match(k) and len(k) <= 80 and len(k.split()) <= 10]
    return kws[:limit] if limit else kws


def volume_all(store: str = settings.store, keywords: list[str] | None = None) -> dict:
    c = DataForSEOClient()
    kws = keywords or _need_keywords(store)
    if not write_retry(lambda con: budget.guard(con, "dataforseo", "volume", len(kws))):
        return {"skipped": "budget", "keywords": len(kws)}
    rows, d = [], today()
    for r in c.search_volume(kws):
        rows.append({"keyword": (r.get("keyword") or "").lower(), "location": store, "search_volume": r.get("search_volume"),
                     "cpc": r.get("cpc"), "competition": COMP.get(str(r.get("competition") or "").upper()),
                     "competition_index": r.get("competition_index"), "monthly": json.dumps(r.get("monthly_searches") or []), "snapshot_date": d})
    rows = list({r["keyword"]: r for r in rows if r["keyword"]}.values())
    write_retry(lambda con: (upsert(con, "keyword_volume", rows),
                             budget.record(con, "dataforseo", "volume", c.tasks, c.spent_usd, f"{len(kws)} keywords")))
    return {"keywords": len(kws), "rows": len(rows), "usd": round(c.spent_usd, 3)}


def seasonality(points: list[tuple], weeks: int | None = None) -> dict:
    """From weekly (date, value) points (all history unless `weeks` is given): strength = sd of month-of-year means
    / overall mean; peak/low month; growth = last 12 months vs all earlier weeks."""
    by_m: dict[int, list[float]] = {}
    vals = []
    for d0, v in (list(points)[-weeks:] if weeks else points):
        try:
            m = int(str(d0)[5:7])
        except ValueError:
            continue
        by_m.setdefault(m, []).append(float(v or 0)); vals.append(float(v or 0))
    if len(by_m) < 12 or not vals or statistics.mean(vals) == 0:
        return {}
    means = {m: statistics.mean(v) for m, v in by_m.items()}
    mu = statistics.mean(means.values())
    last, prior = vals[-52:], vals[:-52]
    growth = (statistics.mean(last) / statistics.mean(prior) - 1) if prior and statistics.mean(prior) > 0 else None
    return {"seasonality": round(statistics.pstdev(means.values()) / mu, 3), "peak_month": max(means, key=means.get),
            "low_month": min(means, key=means.get), "growth_12m_vs_prior": round(growth, 3) if growth is not None else None}


def trends_top(store: str = settings.store, n: int = 1000, reference: str = "calendar app", years: int = 5) -> dict:
    c = DataForSEOClient()
    kws = _need_keywords(store, limit=n)
    if not write_retry(lambda con: budget.guard(con, "dataforseo", "trends", len(kws))):
        return {"skipped": "budget", "keywords": len(kws)}
    d, rows = today(), []
    for t in c.trends(kws, reference, years=years):
        s = seasonality(t["points"])
        growth = s.get("growth_12m_vs_prior")
        rows.append({"keyword": t["keyword"].lower(), "location": store, "reference": reference,
                     "points": json.dumps({"points": t["points"], **s}), "rel_to_reference": t["rel_to_reference"],
                     "slope_12m": growth if growth is not None else t["slope_12m"], "snapshot_date": d})
    rows = list({r["keyword"]: r for r in rows}.values())
    write_retry(lambda con: (upsert(con, "trends", rows),
                             budget.record(con, "dataforseo", "trends", c.tasks, c.spent_usd, f"{len(kws)} keywords {years}y")))
    return {"keywords": len(kws), "rows": len(rows), "usd": round(c.spent_usd, 3)}


def play_rankings(store: str = settings.store, n: int = 300, depth: int = 50) -> dict:
    """Google Play search results via DataForSEO App Data (task_post / tasks_ready / task_get)."""
    import time

    import httpx

    from ..sources.dataforseo import BASE, _get
    c = DataForSEOClient()
    kws = _need_keywords(store, limit=n)
    if not write_retry(lambda con: budget.guard(con, "dataforseo", "play_search", len(kws))):
        return {"skipped": "budget", "keywords": len(kws)}
    pending: dict[str, str] = {}
    for i in range(0, len(kws), 100):
        payload = [{"keyword": k, "location_code": 2840, "language_code": "en", "depth": depth, "priority": 2, "tag": k} for k in kws[i:i + 100]]
        r = httpx.post(f"{BASE}/app_data/google/app_searches/task_post", json=payload, auth=(c.login, c.password), timeout=60)
        r.raise_for_status()
        data = r.json()
        c.spent_usd += float(data.get("cost") or 0)
        for t in data.get("tasks", []):
            if t.get("status_code") in (20100, 20000):
                pending[t["id"]] = (t.get("data") or {}).get("tag")
    d, rows, deadline = today(), [], time.time() + 1800
    while pending and time.time() < deadline:
        time.sleep(10)
        ready = _get(c, "/app_data/google/app_searches/tasks_ready")
        for tid in [x["id"] for t in ready.get("tasks", []) for x in (t.get("result") or []) if x.get("id") in pending]:
            res = _get(c, f"/app_data/google/app_searches/task_get/advanced/{tid}")
            c.spent_usd += float(res.get("cost") or 0)
            kw = pending.pop(tid)
            items = [it for t in res.get("tasks", []) for rr in (t.get("result") or []) for it in (rr.get("items") or [])]
            for j, it in enumerate(items, 1):
                rows.append({"country": store, "keyword": kw, "rank": it.get("rank_absolute") or j, "package": it.get("app_id"),
                             "title": it.get("title"), "developer": (it.get("developer") or {}).get("name") if isinstance(it.get("developer"), dict) else it.get("developer"),
                             "score": (it.get("rating") or {}).get("value") if isinstance(it.get("rating"), dict) else None,
                             "free": it.get("is_free"), "price_text": str((it.get("price") or {}).get("current") if isinstance(it.get("price"), dict) else it.get("price") or ""),
                             "snapshot_date": d})
        log.info("play rankings: %d pending", len(pending))
    rows = list({(r["keyword"], r["rank"]): r for r in rows}.values())
    write_retry(lambda con: (upsert(con, "play_search", rows),
                             budget.record(con, "dataforseo", "play_search", len(kws), c.spent_usd, f"{len(kws)} keywords")))
    return {"keywords": len(kws), "rows": len(rows), "unfinished": len(pending), "usd": round(c.spent_usd, 3)}


def play_pain_points(store: str = settings.store, grades: tuple[str, ...] = ("A", "B"), per_kw: int = 8,
                     depth: int = 150, max_reviews: int = 80, fetch: bool = True) -> dict:
    """Android side of the pain points: for every Build-now niche that has Google Play rankings, fetch the newest
    reviews of its top Play apps (DataForSEO, $0.00075 per app) and let DeepSeek extract fixable complaints
    (factor play_review_gap, meta pain_points). Apps fetched in the last 14 days are reused."""
    import json

    from ..sources.deepseek import DeepSeekClient
    con = connect_read_live()
    try:
        d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='opp_score' AND entity_type='keyword'").fetchone()[0]
        niches = [k for k, m in con.execute("SELECT entity_id, meta FROM factor_values WHERE metric='opp_score' AND entity_type='keyword' "
                                            "AND snapshot_date=? ORDER BY value DESC", [d]).fetchall()
                  if json.loads(m).get("grade") in grades and json.loads(m).get("action") != "No"]
        pk = {}
        for kw, pkg in con.execute("""SELECT keyword, package FROM play_search WHERE country=? AND rank<=? AND package IS NOT NULL
                AND snapshot_date=(SELECT max(snapshot_date) FROM play_search p2 WHERE p2.country=play_search.country AND p2.keyword=play_search.keyword)
                ORDER BY keyword, rank""", [store, per_kw + 2]).fetchall():
            pk.setdefault(kw, []).append(pkg)
        apps_by_kw = {kw: pk[kw][:per_kw] for kw in niches if kw in pk}
        fresh = {r[0] for r in con.execute("SELECT DISTINCT package FROM play_reviews WHERE country=? AND fetched_date>=?",
                                            [store, today() - dt.timedelta(days=14)]).fetchall()}
    finally:
        con.close()
    todo = [p for p in dict.fromkeys(x for v in apps_by_kw.values() for x in v) if p not in fresh]
    log.info("play pain points: %d niches with Play data (of %d), %d apps to fetch", len(apps_by_kw), len(niches), len(todo))
    usd = 0.0
    if fetch and todo:
        c = DataForSEOClient()
        if not write_retry(lambda con: budget.guard(con, "dataforseo", "play_reviews", len(todo))):
            return {"skipped": "budget"}
        rows = []
        for n, (pkg, items) in enumerate(c.play_reviews(todo, depth=depth), 1):
            for it in items:
                rid = it.get("id") or f"{pkg}:{it.get('timestamp')}:{hash(it.get('review_text'))}"
                rows.append({"country": store, "package": pkg, "review_id": str(rid), "rating": (it.get("rating") or {}).get("value"),
                             "body": (it.get("review_text") or "")[:2000], "version": it.get("version"), "helpful": it.get("helpful_count"),
                             "posted": (it.get("timestamp") or "")[:19] or None, "fetched_date": today()})
            if len(rows) >= 2000 or n == len(todo):
                rows = list({(r["package"], r["review_id"]): r for r in rows}.values())
                write_retry(lambda con, rows=rows: upsert(con, "play_reviews", rows))
                rows = []
            if n % 50 == 0:
                log.info("play reviews: %d/%d apps", n, len(todo))
        usd = c.spent_usd
        write_retry(lambda con: budget.record(con, "dataforseo", "play_reviews", len(todo), usd, f"{len(todo)} apps"))
    # DeepSeek: fixable complaints per niche
    ds = DeepSeekClient()
    if not ds.available:
        return {"niches": len(apps_by_kw), "apps_fetched": len(todo), "usd": round(usd, 3), "deepseek": "not configured"}
    con = connect_read_live()
    try:
        done = {r[0] for r in con.execute("SELECT entity_id FROM factor_values WHERE metric='play_review_gap' AND snapshot_date>=?",
                                           [today() - dt.timedelta(days=30)]).fetchall()}
        revs = {}
        for kw, pkgs in apps_by_kw.items():
            if kw in done:
                continue
            q = f"""SELECT rating, body FROM play_reviews WHERE country=? AND package IN ({','.join('?' * len(pkgs))}) AND rating<=3
                    AND length(body) > 15 ORDER BY posted DESC NULLS LAST LIMIT ?"""
            r = con.execute(q, [store, *pkgs, max_reviews]).fetchall()
            if len(r) >= 5:
                revs[kw] = r
    finally:
        con.close()
    out, now = [], dt.datetime.now()
    for i, (kw, r) in enumerate(revs.items(), 1):
        try:
            res = ds.pain_points(kw, [{"rating": x[0], "title": "", "body": x[1][:400]} for x in r])
        except Exception as e:
            log.warning("pain points %s failed: %s", kw, e)
            continue
        out.append({"entity_type": "keyword", "entity_id": kw, "metric": "play_review_gap", "value": float(res.get("opportunity", 0)),
                    "meta": json.dumps({"pain_points": res.get("pain_points", []), "n_reviews": len(r), "platform": "google_play"}),
                    "snapshot_date": today(), "computed_at": now})
        if i % 25 == 0:
            log.info("play pain points: %d/%d niches", i, len(revs))
    if out:
        write_retry(lambda con: (upsert(con, "factor_values", out),
                                 budget.record(con, "deepseek", "play_pain_points", len(out), ds.cost_usd(), f"{len(out)} niches")))
    return {"niches": len(apps_by_kw), "apps_fetched": len(todo), "usd": round(usd, 3), "pain_points": len(out), "deepseek_usd": round(ds.cost_usd(), 3)}
