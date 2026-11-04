"""Collection jobs. Each writes a dated snapshot and a collection_log row."""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable

from .. import budget
from ..config import APPLE_GENRES, settings
from ..db import log_job, today, upsert
from ..sources.astro import AstroClient
from ..sources.googleplay import PlayClient, details_to_row
from ..sources.itunes import ITunesClient, lookup_to_row

log = logging.getLogger("apptopics.collect")
RESEARCH_APP = "AppTopicsQuants Research"


def _job(con, name: str, store: str, fn: Callable[[], int]) -> int:
    started = dt.datetime.now()
    try:
        n = fn()
        log_job(con, name, store, started, n, True)
        log.info("%s: %d rows", name, n)
        return n
    except Exception as e:  # keep other jobs running
        log_job(con, name, store, started, 0, False, repr(e)[:500])
        log.exception("%s failed", name)
        return 0


# -- keyword universe --------------------------------------------------------
def collect_keywords(con, store: str = settings.store, max_pages: int | None = None) -> int:
    def run():
        astro, d, rows = AstroClient(), today(), []
        for rank, k in astro.iter_most_searched(store, max_pages=max_pages):
            rows.append({"store": store, "keyword": k["text"].strip().lower(), "genre": k.get("genre"),
                         "popularity": k.get("popularity"), "rank_in_store": rank, "snapshot_date": d})
        # dedupe (same text can appear under two genres): keep highest popularity
        best: dict[str, dict] = {}
        for r in rows:
            if r["keyword"] not in best or r["popularity"] > best[r["keyword"]]["popularity"]:
                best[r["keyword"]] = r
        return upsert(con, "keywords", list(best.values()))
    return _job(con, "keywords", store, run)


def selected(con, store: str, top: int, brand_filter: bool = True) -> list[str]:
    """Top keywords by popularity, dropping likely brands when brand_filter."""
    mb = settings.weights()["filters"]["max_brandness"] if brand_filter else None
    return select_keywords(con, store, top, max_brandness=mb)


def ranked(con, top: int, entity: str = "keyword") -> list[str]:
    """Top-N non-excluded entities by latest composite; before the first scoring, popularity order."""
    import json
    rows = con.execute("""SELECT entity_id, value, meta FROM factor_values WHERE metric='composite' AND entity_type=?
                          QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1 ORDER BY value DESC""", [entity]).fetchall()
    out = [k for k, v, m in rows if not (json.loads(m) if m else {}).get("excluded")][:top]
    return out or (selected(con, settings.store, top) if entity == "keyword" else [])


def is_latin(keyword: str) -> bool:
    return all(ord(ch) < 0x250 for ch in keyword)   # ASCII + Latin-1 + Latin Extended


def select_keywords(con, store: str, top_n: int, max_brandness: float | None = None,
                    min_popularity: int = 5) -> list[str]:
    """Top keywords by latest popularity, dropping likely brands (latest brandness factor, when one
    exists) and, with [filters].latin_only, non-Latin-script terms."""
    sql = """
      WITH m AS (SELECT keyword, popularity FROM keyword_metrics WHERE popularity IS NOT NULL
                 QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1),
           k AS (SELECT k0.keyword, coalesce(k0.popularity, m.popularity) AS popularity FROM keywords k0 LEFT JOIN m USING (keyword)
                 WHERE k0.store=? AND k0.snapshot_date=(SELECT max(snapshot_date) FROM keywords WHERE store=?)),  -- long tail: Astro popularity
           b AS (SELECT entity_id keyword, value brand FROM factor_values WHERE entity_type='keyword' AND metric='brandness'
                 QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1)
      SELECT k.keyword FROM k LEFT JOIN b USING (keyword)
      WHERE popularity>=? AND (? IS NULL OR brand IS NULL OR brand <= ?)
      ORDER BY popularity DESC, keyword"""
    rows = [r[0] for r in con.execute(sql, [store, store, min_popularity, max_brandness, max_brandness]).fetchall()]
    f = settings.weights().get("filters", {})
    if f.get("latin_only", True):
        rows = [k for k in rows if is_latin(k)]
    excl = [t.lower() for t in f.get("exclude_terms", [])]
    if excl:
        rows = [k for k in rows if not any(t in k for t in excl)]
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='keyword_labels'").fetchone()[0]:
        hide = set(f.get("hide_labels", ["brand", "rubbish"]))
        bad = {k for k, lab in con.execute("SELECT keyword, label FROM keyword_labels WHERE store=?", [store]).fetchall() if lab in hide}
        rows = [k for k in rows if k not in bad]
    return rows[:top_n]


# -- app store search pages ----------------------------------------------------
def collect_search(con, keywords: list[str], store: str = settings.store, limit: int = 100, platform: str = "iphone") -> int:
    def run():
        astro, d, n = AstroClient(), today(), 0
        for i, kw in enumerate(keywords, 1):
            try:
                data = astro.search(kw, store, limit=limit, platform=platform)
            except Exception as e:
                log.warning("search %r failed: %s", kw, e)
                continue
            rows = [{"store": store, "platform": platform, "keyword": kw, "rank": a.get("ranking", j),
                     "app_id": str(a.get("appStoreId")), "name": a.get("name"), "developer": a.get("developer"),
                     "genre": a.get("genre"), "rating_count": a.get("ratingCount"), "avg_rating": a.get("averageRating"),
                     "subtitle": a.get("subtitle"), "total_results": data.get("totalResults"), "snapshot_date": d}
                    for j, a in enumerate(data.get("apps", []), 1)]
            n += upsert(con, "search_results", rows)
            if i % 25 == 0:
                log.info("search %d/%d", i, len(keywords))
        return n
    return _job(con, "search", store, run)


# -- charts --------------------------------------------------------------------
def collect_charts(con, store: str = settings.store, charts=("grossing", "free", "paid")) -> int:
    def run():
        it, d, n = ITunesClient(), today(), 0
        genres: list[str | None] = [None, *list(APPLE_GENRES)]
        for chart in charts:
            for g in genres:
                try:
                    entries = it.chart(chart, g, country=store)
                except Exception as e:
                    log.warning("chart %s/%s failed: %s", chart, g, e)
                    continue
                rows = [{"store": store, "chart": chart, "genre_id": g or "all", **e, "snapshot_date": d} for e in entries]
                n += upsert(con, "chart_entries", rows)
        return n
    return _job(con, "charts", store, run)


# -- app metadata ----------------------------------------------------------------
def collect_apps(con, store: str = settings.store, max_age_days: int = 7, limit: int | None = None) -> int:
    """iTunes lookup for every app id seen in search results or charts whose metadata
    is older than max_age_days (or missing)."""
    def run():
        it, d = ITunesClient(), today()
        ids = [r[0] for r in con.execute("""
            WITH seen AS (SELECT DISTINCT app_id FROM search_results WHERE store=?
                          UNION SELECT DISTINCT app_id FROM chart_entries WHERE store=?),
                 fresh AS (SELECT app_id FROM apps WHERE store=? AND snapshot_date >= ?)
            SELECT app_id FROM seen WHERE app_id IS NOT NULL AND app_id NOT IN (SELECT app_id FROM fresh)""",
            [store, store, store, d - dt.timedelta(days=max_age_days)]).fetchall()]
        if limit:
            ids = ids[:limit]
        n = 0
        for i in range(0, len(ids), 150):
            chunk = ids[i:i + 150]
            try:
                res = it.lookup(chunk, country=store)
            except Exception as e:
                log.warning("lookup chunk failed: %s", e)
                continue
            n += upsert(con, "apps", [lookup_to_row(r, store, d) for r in res if r.get("wrapperType") == "software"])
        return n
    return _job(con, "apps", store, run)


def collect_apps_for(con, app_ids: list[str], store: str = settings.store) -> int:
    """iTunes lookup for an explicit id list (always refreshed; used by the daily light run)."""
    def run():
        it, d, n = ITunesClient(), today(), 0
        for i in range(0, len(app_ids), 150):
            res = it.lookup(app_ids[i:i + 150], country=store)
            n += upsert(con, "apps", [lookup_to_row(r, store, d) for r in res if r.get("wrapperType") == "software"])
        return n
    return _job(con, "apps_band", store, run)


# -- google play -----------------------------------------------------------------
def collect_play(con, keywords: list[str], country: str = settings.store, num: int = 20, details_top: int = 10,
                 max_age_days: int = 7) -> int:
    def run():
        gp, d, n = PlayClient(country=country), today(), 0
        packages: list[str] = []
        for i, kw in enumerate(keywords, 1):
            try:
                res = gp.search(kw, num=num)
            except Exception as e:
                log.warning("play search %r failed: %s", kw, e)
                continue
            rows = [{"country": country, "keyword": kw, "rank": j, "package": r.get("app_id"), "title": r.get("title"),
                     "developer": r.get("developer"), "score": r.get("score"), "free": r.get("free"),
                     "price_text": r.get("price_text"), "snapshot_date": d} for j, r in enumerate(res, 1)]
            n += upsert(con, "play_search", rows)
            packages += [r["package"] for r in rows[:details_top]]
            if i % 25 == 0:
                log.info("play search %d/%d", i, len(keywords))
        fresh = {r[0] for r in con.execute("SELECT package FROM play_apps WHERE country=? AND snapshot_date>=?",
                                            [country, d - dt.timedelta(days=max_age_days)]).fetchall()}
        todo = [p for p in dict.fromkeys(packages) if p and p not in fresh]
        log.info("play details for %d packages", len(todo))
        for i, pkg in enumerate(todo, 1):
            try:
                det = gp.details(pkg)
            except Exception as e:
                log.warning("play details %s failed: %s", pkg, e)
                continue
            if det:
                n += upsert(con, "play_apps", [details_to_row(det, country, d)])
            if i % 100 == 0:
                log.info("play details %d/%d", i, len(todo))
        return n
    return _job(con, "play", country, run)


# -- astro difficulty via a research placeholder app ---------------------------------
def collect_difficulty(con, keywords: list[str], store: str = settings.store) -> int:
    """Astro only exposes difficulty for tracked keywords. We track them under a
    temporary placeholder app named RESEARCH_APP (visible in Astro; harmless)."""
    def run():
        astro, d = AstroClient(), today()
        astro.ensure_placeholder_app(RESEARCH_APP, platform="iphone", store=store)
        for i in range(0, len(keywords), 100):
            try:
                astro.add_keywords(RESEARCH_APP, keywords[i:i + 100], store=store)
            except Exception as e:
                log.warning("add_keywords chunk failed: %s", e)
            time.sleep(0.5)
        data = astro.app_keywords(app_name=RESEARCH_APP)
        rows = [{"store": store, "keyword": k["keyword"].strip().lower(), "popularity": k.get("popularity"),
                 "difficulty": k.get("difficulty"), "apps_count": k.get("appsCount"), "source": "astro_tracked",
                 "snapshot_date": d} for k in data.get("keywords", []) if k.get("store", store) == store]
        return upsert(con, "keyword_metrics", rows)
    return _job(con, "difficulty", store, run)


# -- LLM jobs (DeepSeek) ------------------------------------------------------------------------
def _llm_job(con, name: str, store: str, n_items: int, fn):
    """Shared wrapper: availability check, budget guard, run fn(ds) -> rows written, record real spend."""
    from ..sources.deepseek import DeepSeekClient

    def run():
        ds = DeepSeekClient()
        if not ds.available:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        if not budget.guard(con, "deepseek", name, n_items):
            return 0
        n = fn(ds)
        budget.record(con, "deepseek", name, ds.usage_in + ds.usage_out, ds.cost_usd(), f"{n_items} items")
        return n
    return _job(con, name, store, run)


def _factor_rows(metric: str, values: dict[str, float], metas: dict[str, dict] | None = None) -> list[dict]:
    import json
    d, now = today(), dt.datetime.now()
    return [{"entity_type": "keyword", "entity_id": k, "metric": metric, "value": float(v),
             "meta": json.dumps(metas[k]) if metas and k in metas else None, "snapshot_date": d, "computed_at": now} for k, v in values.items()]


def collect_llm_brands(con, keywords: list[str], store: str = settings.store) -> int:
    return _llm_job(con, "llm_brands", store, len(keywords),
                    lambda ds: upsert(con, "factor_values", _factor_rows("llm_brand", ds.classify_brands(keywords))))


def collect_llm_labels(store: str = settings.store, relabel: bool = False, workers: int = 8, chunk: int = 40) -> dict:
    """Label every universe keyword need/brand/vague/rubbish with DeepSeek, giving it the top-3 app names as
    context. Reads with a read-only connection, calls the API without holding the write lock (results are
    cached in data/cache/llm_labels.jsonl so a crash resumes), then writes keyword_labels + llm_brand."""
    import json
    from concurrent.futures import ThreadPoolExecutor

    from ..db import connect_read_live
    from ..sources.deepseek import DeepSeekClient

    ds = DeepSeekClient()
    if not ds.available:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    cache_p = settings.db_path.parent / "cache" / "llm_labels.jsonl"
    cache_p.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_p.exists() and not relabel:
        for line in cache_p.read_text().splitlines():
            if line.strip():
                r = json.loads(line); cache[r["term"]] = r
    con = connect_read_live()
    try:
        kws = [k for (k,) in con.execute("SELECT keyword FROM keywords WHERE store=? AND snapshot_date=(SELECT max(snapshot_date) FROM keywords WHERE store=?) ORDER BY popularity DESC", [store, store]).fetchall()]
        done = set() if relabel else {k for (k,) in con.execute("SELECT keyword FROM keyword_labels WHERE store=?", [store]).fetchall()} if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='keyword_labels'").fetchone()[0] else set()
        tops: dict[str, list[str]] = {}
        for kw, name in con.execute("""SELECT keyword, name FROM search_results WHERE store=? AND rank<=3
                QUALIFY dense_rank() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1 ORDER BY keyword, rank""", [store]).fetchall():
            tops.setdefault(kw, []).append(name)
    finally:
        con.close()
    todo = [k for k in kws if k not in done and k not in cache]
    est = budget.estimate_job("deepseek", "llm_labels", len(todo))
    log.info("llm labels: %d keywords to label (est $%.3f), %d cached", len(todo), est, len(cache))
    from .discover import write_retry
    if todo and not write_retry(lambda c: budget.guard(c, "deepseek", "llm_labels", len(todo))):
        return {"skipped": "budget", "new": len(todo), "est_usd": round(est, 3)}
    batches = [todo[i:i + chunk] for i in range(0, len(todo), chunk)]
    lock = __import__("threading").Lock()

    def run(batch):
        try:
            res = ds.label_keywords([{"term": k, "top_apps": tops.get(k, [])} for k in batch])
        except Exception as e:
            log.warning("label batch failed: %r", e)
            return 0
        with lock, cache_p.open("a") as f:
            for k in batch:
                if k in res:
                    r = {"term": k, **res[k]}; cache[k] = r; f.write(json.dumps(r) + "\n")
        return len(res)
    with ThreadPoolExecutor(workers) as ex:
        for i, _n in enumerate(ex.map(run, batches), 1):
            if i % 20 == 0:
                log.info("llm labels: %d/%d batches", i, len(batches))
    now = dt.datetime.now()
    rows = [{"store": store, "keyword": k, "label": r["label"], "p_brand": r["p_brand"], "reason": r["reason"],
             "model": ds.model, "labeled_at": now} for k, r in cache.items() if k in set(kws)]

    def _w(wcon):
        upsert(wcon, "keyword_labels", rows)
        upsert(wcon, "factor_values", _factor_rows("llm_brand", {r["keyword"]: r["p_brand"] for r in rows}))
        budget.record(wcon, "deepseek", "llm_labels", ds.usage_in + ds.usage_out, ds.cost_usd(), f"{len(todo)} keywords")
    write_retry(_w)
    from collections import Counter
    return {"labeled": len(rows), "new": len(todo), "usd": round(ds.cost_usd(), 4), "labels": dict(Counter(r["label"] for r in rows))}


def collect_llm_buildability(con, keywords: list[str], store: str = settings.store) -> int:
    constraints = settings.weights().get("constraints", {}).get("description", "")

    def fn(ds):
        res = ds.buildability(keywords, constraints)
        return upsert(con, "factor_values", _factor_rows("buildability", {k: v["score"] for k, v in res.items()},
                                                         {k: {"reason": v["reason"]} for k, v in res.items()}))
    return _llm_job(con, "llm_buildability", store, len(keywords), fn)


def collect_llm_votes(con, keywords: list[str], store: str = settings.store) -> int:
    def fn(ds):
        d, n = today(), 0
        for kw in keywords:
            apps = [r[0] for r in con.execute("""SELECT name FROM search_results WHERE store=? AND keyword=? AND rank<=5
                AND snapshot_date=(SELECT max(snapshot_date) FROM search_results WHERE store=? AND keyword=?) ORDER BY rank""",
                [store, kw, store, kw]).fetchall()]
            votes = ds.consumer_vote(kw, f"Apps currently ranking: {', '.join(apps)}", [kw])
            n += upsert(con, "llm_votes", [{"entity_type": "keyword", "entity_id": kw, "model": ds.model, **v, "snapshot_date": d} for v in votes])
        return n
    return _llm_job(con, "llm_votes", store, len(keywords), fn)


def collect_llm_pain_points(con, keywords: list[str], store: str = settings.store, max_reviews: int = 60) -> int:
    def fn(ds):
        values, metas = {}, {}
        for kw in keywords:
            ids = band_app_ids(con, store, [kw])
            if not ids:
                continue
            revs = con.execute(f"""SELECT rating, title, body FROM reviews WHERE store=? AND app_id IN ({','.join('?'*len(ids))})
                                   AND rating<=3 ORDER BY updated DESC LIMIT ?""", [store, *ids, max_reviews]).fetchall()
            if len(revs) < 5:
                continue
            res = ds.pain_points(kw, [{"rating": r[0], "title": r[1], "body": (r[2] or "")[:400]} for r in revs])
            values[kw] = float(res.get("opportunity", 0))
            metas[kw] = {"pain_points": res.get("pain_points", []), "n_reviews": len(revs)}
        return upsert(con, "factor_values", _factor_rows("review_gap", values, metas))
    return _llm_job(con, "llm_pain_points", store, len(keywords), fn)


def collect_topics(con, keywords: list[str], store: str = settings.store, chunk: int = 150) -> int:
    def fn(ds):
        existing = {r[0]: r[1] for r in con.execute("SELECT topic_id, label FROM topics").fetchall()}
        n, now = 0, dt.datetime.now()
        for i in range(0, len(keywords), chunk):
            batch = keywords[i:i + chunk]
            for t in ds.cluster_topics(batch, known_labels=list(existing.values())[:300]):
                tid = slugify(t.get("label", ""))
                if not tid:
                    continue
                if tid not in existing:
                    upsert(con, "topics", [{"topic_id": tid, "label": t.get("label"), "description": t.get("description"), "created_at": now}])
                    existing[tid] = t.get("label")
                n += upsert(con, "topic_keywords", [{"topic_id": tid, "store": store, "keyword": k.strip().lower(), "weight": 1.0}
                                                    for k in t.get("keywords", []) if k.strip().lower() in batch])
            log.info("topics %d/%d keywords -> %d topics", min(i + chunk, len(keywords)), len(keywords), len(existing))
        return n
    return _llm_job(con, "topics", store, len(keywords), fn)


def slugify(label: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60]

def collect_asc(con, days: int = 90) -> int:
    """Pull daily SALES summary reports for the last N days (skips days already stored or not yet published)."""
    from ..sources.asc import ASCClient, sales_rows_to_db

    def run():
        asc = ASCClient()
        if not asc.available:
            raise RuntimeError("ASC_* not configured in .env (issuer id, key id, .p8 path, vendor number)")
        have = {r[0] for r in con.execute("SELECT DISTINCT report_date FROM own_sales").fetchall()}
        n = 0
        for i in range(1, days + 1):
            day = today() - dt.timedelta(days=i)
            if day in have:
                continue
            rows = asc.daily_sales(day)
            if rows is None:
                continue
            db_rows = sales_rows_to_db(rows, day)
            con.execute("DELETE FROM own_sales WHERE report_date=?", [day])
            n += upsert(con, "own_sales", [{**r, "row_no": j} for j, r in enumerate(db_rows)])
            time.sleep(0.3)
        return n
    return _job(con, "asc_sales", "own", run)


# -- DataForSEO: Google Ads volume/CPC and Google Trends ------------------------------
def collect_volume(con, keywords: list[str], location: str = "us") -> int:
    import json

    from ..sources.dataforseo import DataForSEOClient

    def run():
        c = DataForSEOClient()
        if not c.available:
            raise RuntimeError("DATAFORSEO_LOGIN/PASSWORD not set")
        if not budget.guard(con, "dataforseo", "volume", len(keywords)):
            return 0
        d, rows = today(), []
        for r in c.search_volume(keywords):
            rows.append({"keyword": (r.get("keyword") or "").lower(), "location": location, "search_volume": r.get("search_volume"),
                         "cpc": r.get("cpc"), "competition": r.get("competition"), "competition_index": r.get("competition_index"),
                         "monthly": json.dumps(r.get("monthly_searches") or []), "snapshot_date": d})
        budget.record(con, "dataforseo", "volume", c.tasks, c.spent_usd, f"{len(keywords)} keywords")
        return upsert(con, "keyword_volume", rows)
    return _job(con, "volume", location, run)


def collect_trends(con, keywords: list[str], location: str = "us", reference: str = "calendar app") -> int:
    import json

    from ..sources.dataforseo import DataForSEOClient

    def run():
        c = DataForSEOClient()
        if not c.available:
            raise RuntimeError("DATAFORSEO_LOGIN/PASSWORD not set")
        if not budget.guard(con, "dataforseo", "trends", len(keywords)):
            return 0
        d, rows = today(), []
        for t in c.trends(keywords, reference):
            rows.append({"keyword": t["keyword"].lower(), "location": location, "reference": reference, "points": json.dumps(t["points"]),
                         "rel_to_reference": t["rel_to_reference"], "slope_12m": t["slope_12m"], "snapshot_date": d})
        budget.record(con, "dataforseo", "trends", c.tasks, c.spent_usd, f"{len(keywords)} keywords vs {reference}")
        return upsert(con, "trends", rows)
    return _job(con, "trends", location, run)


# -- Apple customer reviews for the apps in each keyword's p5-p20 band ------------------------
def band_app_ids(con, store: str, keywords: list[str]) -> list[str]:
    """App ids inside the p5-p20 band of the given keywords (needs search snapshots)."""
    from ..metrics.sector import _target_cfg, band, sector_estimates
    _, (lo, hi), _ = _target_cfg()
    est = sector_estimates(con, store, today())
    ids: dict[str, None] = {}
    for kw in keywords:
        for a in band(est.get(kw, []), lo, hi):
            ids.setdefault(a.app_id)
    return list(ids)


def collect_reviews(con, app_ids: list[str], store: str = settings.store, pages: int = 2, max_age_days: int = 14) -> int:
    def run():
        it, d, n = ITunesClient(), today(), 0
        fresh = {r[0] for r in con.execute("SELECT DISTINCT app_id FROM reviews WHERE store=? AND fetched_date>=?",
                                            [store, d - dt.timedelta(days=max_age_days)]).fetchall()}
        todo = [a for a in app_ids if a not in fresh]
        for i, app_id in enumerate(todo, 1):
            try:
                revs = it.reviews(app_id, country=store, pages=pages)
            except Exception as e:
                log.warning("reviews %s failed: %s", app_id, e)
                continue
            rows = []
            for r in revs:
                upd = r.get("updated")
                try:
                    upd = dt.datetime.fromisoformat(upd).replace(tzinfo=None) if upd else None
                except ValueError:
                    upd = None
                rows.append({"store": store, "app_id": app_id, **{k: r[k] for k in ("review_id", "rating", "title", "body", "version")},
                             "updated": upd, "fetched_date": d})
            n += upsert(con, "reviews", rows)
            if i % 50 == 0:
                log.info("reviews %d/%d", i, len(todo))
        return n
    return _job(con, "reviews", store, run)


# -- Google Play charts ---------------------------------------------------------------------
def collect_play_charts(con, country: str = settings.store, collections=("GROSSING", "TOP_FREE", "TOP_PAID")) -> int:
    def run():
        gp, d, n = PlayClient(country=country), today(), 0
        for coll in collections:
            for cat in gp.categories():
                try:
                    res = gp.chart(coll, cat, num=100)
                except Exception as e:
                    log.warning("play chart %s/%s failed: %s", coll, cat, e)
                    continue
                n += upsert(con, "play_charts", [{"country": country, "collection": coll, "category": cat, "rank": j, "package": r.get("app_id"),
                                                  "title": r.get("title"), "developer": r.get("developer"), "free": r.get("free"), "snapshot_date": d}
                                                 for j, r in enumerate(res, 1)])
        return n
    return _job(con, "play_charts", country, run)


# -- Social buzz --------------------------------------------------------------------------------
# -- Apple Ads Platform API: weekly popularity (trending source + universe) ----------------------
def collect_popularity(con, country: str = settings.store, weeks: int = 26) -> int:
    """Pull weekly search-term popularity for the last N complete weeks (skips weeks already stored,
    stops when Apple returns nothing for an older week)."""
    from ..sources.appleads import AppleAdsClient, rows_to_db, week_start

    def run():
        c = AppleAdsClient()
        if not c.available:
            raise RuntimeError("APPLE_ADS_* not configured")
        have = {r[0] for r in con.execute("SELECT DISTINCT week FROM popularity_weekly WHERE country=?", [country]).fetchall()}
        last_sat = week_start(today()) - dt.timedelta(days=1)
        n, empty_streak = 0, 0
        for i in range(weeks):
            end = last_sat - dt.timedelta(days=7 * i)
            start = end - dt.timedelta(days=6)
            if start in have:
                continue
            rows = c.search_term_popularity(country, start, end)
            if not rows:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            n += upsert(con, "popularity_weekly", rows_to_db(rows, country))
            log.info("popularity week %s: %d rows", start, len(rows))
        return n
    return _job(con, "popularity", country, run)


def keywords_from_popularity(con, store: str = settings.store) -> int:
    """Refresh the keyword universe snapshot from the latest Apple Ads week (replaces the Astro list
    when available: same dataset, no rate limit, with genre and history)."""
    wk = con.execute("SELECT max(week) FROM popularity_weekly WHERE country=?", [store]).fetchone()[0]
    if wk is None:
        return 0
    rows = con.execute("""
        SELECT term, arg_max(genre, pop_1to100), max(pop_1to100) FROM popularity_weekly
        WHERE country=? AND week=? GROUP BY term ORDER BY 3 DESC""", [store, wk]).fetchall()
    d = today()
    return upsert(con, "keywords", [{"store": store, "keyword": t, "genre": g, "popularity": p, "rank_in_store": i, "snapshot_date": d}
                                    for i, (t, g, p) in enumerate(rows, 1)])


# -- App Store search pages via DataForSEO (fast, parallel, paid) ------------------------------------
def collect_search_dataforseo(con, keywords: list[str], store: str = settings.store, depth: int = 100, priority: int = 2) -> int:
    from ..sources.dataforseo import DataForSEOClient

    def run():
        c = DataForSEOClient()
        if not c.available:
            raise RuntimeError("DATAFORSEO_LOGIN/PASSWORD not set")
        if not budget.guard(con, "dataforseo", "search", len(keywords)):
            return 0
        d, n = today(), 0
        for kw, items in c.app_searches(keywords, depth=depth, priority=priority):
            rows = []
            for a in items:
                if a.get("type") != "app_item":
                    continue
                rating = a.get("rating") or {}
                rows.append({"store": store, "platform": "iphone", "keyword": kw, "rank": a.get("rank_absolute") or a.get("rank_group"),
                             "app_id": str(a.get("app_id")), "name": a.get("title"), "developer": a.get("developer"),
                             "genre": None, "rating_count": rating.get("votes_count"), "avg_rating": rating.get("value"),
                             "subtitle": None, "total_results": len(items), "snapshot_date": d})
            n += upsert(con, "search_results", rows)
        budget.record(con, "dataforseo", "search", c.tasks, c.spent_usd, f"{len(keywords)} keywords depth {depth} prio {priority}")
        return n
    return _job(con, "search_dataforseo", store, run)


# -- App Store search pages via the free iTunes Search API (metadata included, ~20 req/min) ----------
def collect_search_itunes(con, keywords: list[str], store: str = settings.store, limit: int = 200) -> int:
    from ..sources.itunes import lookup_to_row

    def run():
        it, d, n = ITunesClient(), today(), 0
        for i, kw in enumerate(keywords, 1):
            try:
                res = it.search(kw, country=store, limit=limit)
            except Exception as e:
                log.warning("itunes search %r failed: %s", kw, e)
                continue
            rows = [{"store": store, "platform": "iphone", "keyword": kw, "rank": j, "app_id": str(r.get("trackId")), "name": r.get("trackName"),
                     "developer": r.get("sellerName") or r.get("artistName"), "genre": r.get("primaryGenreName"), "rating_count": r.get("userRatingCount"),
                     "avg_rating": r.get("averageUserRating"), "subtitle": None, "total_results": len(res), "snapshot_date": d}
                    for j, r in enumerate(res, 1) if r.get("wrapperType") == "software"]
            n += upsert(con, "search_results", rows)
            upsert(con, "apps", [lookup_to_row(r, store, d) for r in res if r.get("wrapperType") == "software"])
            if i % 25 == 0:
                log.info("itunes search %d/%d", i, len(keywords))
        return n
    return _job(con, "search_itunes", store, run)


def collect_search_auto(con, keywords: list[str], store: str = settings.store, backend: str | None = None) -> int:
    """Pick the search backend: config [sources].search_backend or 'auto' (dataforseo if configured, else astro)."""
    from ..sources.dataforseo import DataForSEOClient
    backend = backend or settings.weights().get("sources", {}).get("search_backend", "auto")
    if backend == "auto":
        from .weekly import astro_up
        backend = "dataforseo" if DataForSEOClient().available else ("astro" if astro_up() else "itunes")
    log.info("search backend: %s (%d keywords)", backend, len(keywords))
    if backend == "dataforseo":
        return collect_search_dataforseo(con, keywords, store)
    if backend == "itunes":
        return collect_search_itunes(con, keywords, store)
    return collect_search(con, keywords, store, limit=100)


# -- long-tail discovery -------------------------------------------------------------------------------
def collect_longtail(store: str = settings.store, seeds_limit: int | None = None, per_seed: int = 6,
                     use_hints: bool = True, use_llm: bool = True) -> dict:
    """Expand 'need' keywords into long-tail searches: App Store autocomplete (real searches) + DeepSeek variants.
    Writes longtail_keywords and merges new terms into the current keyword universe snapshot (popularity NULL
    until Astro fills it). API calls run without holding the DB write lock."""
    from ..db import connect_read_live
    from ..sources.deepseek import DeepSeekClient
    from ..sources.itunes import ITunesClient
    con = connect_read_live()
    try:
        seeds = [k for (k,) in con.execute("""SELECT l.keyword FROM keyword_labels l JOIN keywords k ON k.keyword=l.keyword AND k.store=l.store
                 AND k.snapshot_date=(SELECT max(snapshot_date) FROM keywords WHERE store=?)
                 WHERE l.store=? AND l.label='need' ORDER BY k.popularity DESC""", [store, store]).fetchall()]
        known = {k for (k,) in con.execute("SELECT DISTINCT keyword FROM keywords WHERE store=?", [store]).fetchall()}
        if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='longtail_keywords'").fetchone()[0]:
            known |= {k for (k,) in con.execute("SELECT keyword FROM longtail_keywords WHERE store=?", [store]).fetchall()}
        snap = con.execute("SELECT max(snapshot_date) FROM keywords WHERE store=?", [store]).fetchone()[0]
    finally:
        con.close()
    seeds = [k for k in seeds if is_latin(k)][: seeds_limit or None]
    found: dict[str, dict] = {}
    if use_hints:
        it = ITunesClient()
        for i, sd in enumerate(seeds, 1):
            try:
                for rank, h in enumerate(it.hints(sd), 1):
                    if h != sd and h not in known and h not in found and is_latin(h):
                        found[h] = {"seed": sd, "source": "hints", "hint_rank": rank}
            except Exception as e:
                log.warning("hints %r failed: %s", sd, e)
            if i % 100 == 0:
                log.info("hints: %d/%d seeds, %d new terms", i, len(seeds), len(found))
    usd = 0.0
    if use_llm:
        ds = DeepSeekClient()
        if ds.available:
            for sd, vs in ds.longtail_variants(seeds, per_seed).items():
                for v in vs:
                    if v != sd and v not in known and v not in found and is_latin(v) and 2 <= len(v.split()) <= 6:
                        found[v] = {"seed": sd, "source": "llm", "hint_rank": None}
            usd = ds.cost_usd()
    d = today()
    rows = [{"store": store, "keyword": k, "seed": v["seed"], "source": v["source"], "hint_rank": v["hint_rank"],
             "popularity": None, "discovered_at": d} for k, v in found.items()]
    from .discover import write_retry

    def _w(w):
        upsert(w, "longtail_keywords", rows)
        upsert(w, "keywords", [{"store": store, "keyword": r["keyword"], "genre": None, "popularity": None,
                                "rank_in_store": None, "snapshot_date": snap} for r in rows])
        if usd:
            budget.record(w, "deepseek", "longtail", ds.usage_in + ds.usage_out, usd, f"{len(seeds)} seeds")
    write_retry(_w)
    from collections import Counter
    return {"seeds": len(seeds), "new_terms": len(rows), "by_source": dict(Counter(r["source"] for r in rows)), "usd": round(usd, 4)}


def finish_longtail(store: str = settings.store, min_pop: int = 15, max_search: int | None = None) -> dict:
    """After collect_longtail: label new terms, Astro popularity for the 'need' ones, iTunes competitor pages for
    those with popularity >= min_pop, then rescore. Lock-friendly (short write transactions)."""
    from ..db import connect_read_live
    from .discover import astro_popularity, label_terms, search_terms, write_retry
    con = connect_read_live()
    try:
        terms = [k for (k,) in con.execute("SELECT keyword FROM longtail_keywords WHERE store=?", [store]).fetchall()]
    finally:
        con.close()
    labels = label_terms(terms, store)
    need = [t for t in terms if labels.get(t, {}).get("label") == "need"]
    log.info("longtail: %d terms, %d need", len(terms), len(need))
    con = connect_read_live()
    try:
        have = {k: p for k, p in con.execute("""SELECT keyword, popularity FROM keyword_metrics WHERE popularity IS NOT NULL
                AND keyword IN (SELECT unnest(?)) QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [need]).fetchall()}
        searched = {k for (k,) in con.execute("SELECT DISTINCT keyword FROM search_results WHERE keyword IN (SELECT unnest(?))", [need]).fetchall()}
    finally:
        con.close()
    todo = [t for t in need if t not in have]
    for i in range(0, len(todo), 500):          # chunks so progress is saved as we go
        got = astro_popularity(todo[i:i + 500], store)
        have.update({k: v["popularity"] for k, v in got.items() if v.get("popularity") is not None})
        log.info("astro popularity: %d/%d", min(i + 500, len(todo)), len(todo))
    to_search = sorted([t for t in need if (have.get(t) or 0) >= min_pop and t not in searched], key=lambda t: -have[t])
    if max_search:
        to_search = to_search[:max_search]
    log.info("searching %d long-tail terms with popularity >= %d", len(to_search), min_pop)
    n = search_terms(to_search, store, progress=lambda i, t: log.info("itunes search %d/%d", i, t) if i % 50 == 0 else None)
    from .score import score_all
    counts = write_retry(lambda c: score_all(c, store))
    return {"terms": len(terms), "need": len(need), "with_popularity": len(have), "searched": n, "index": counts.get("index")}
