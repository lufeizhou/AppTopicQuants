"""Building blocks shared by the long-tail pipeline and on-demand research.

Every step calls external APIs WITHOUT holding the DuckDB write lock and writes in short transactions
(write_retry), so a multi-hour collection and an on-demand research run can interleave. Astro calls are
serialized machine-wide (it crashes under parallel load); iTunes calls share one machine-wide rate limit."""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from collections.abc import Callable

from .. import budget
from ..config import settings
from ..db import connect, connect_read_live, today, upsert
from ..sources.base import machine_lock

log = logging.getLogger("apptopics.discover")
RESEARCH_APP = "AppTopicsQuants Research"
_APPNAME = re.compile(r"[:•·–|]| - |\bby\b")


def write_retry(fn: Callable, tries: int = 120, wait: float = 5.0):
    """Run fn(con) in a short write transaction, retrying while another process holds the DuckDB lock."""
    import duckdb
    for i in range(tries):
        try:
            with connect() as con:
                return fn(con)
        except (duckdb.IOException, duckdb.ConnectionException) as e:
            if i == tries - 1:
                raise
            if i % 12 == 0:
                log.info("database busy, retrying (%s)", e.__class__.__name__)
            time.sleep(wait)


def looks_like_app_name(term: str) -> bool:
    return bool(_APPNAME.search(term)) or len(term) > 45


def label_terms(terms: list[str], store: str = settings.store) -> dict[str, dict]:
    """DeepSeek type (need/brand/rubbish) + specificity labels for terms not labelled yet (cached in keyword_labels).
    Uses the top-3 apps of a results page when one exists (context 'top_apps'), else the string alone ('string');
    `apptopics llm labels --scope context` relabels string-only terms once their page arrives."""
    from concurrent.futures import ThreadPoolExecutor

    from ..sources.deepseek import DeepSeekClient
    con = connect_read_live()
    try:
        have = {k: {"label": l, "p_brand": p, "specificity": s} for k, l, p, s in con.execute(
            "SELECT keyword, label, p_brand, specificity FROM keyword_labels WHERE store=?", [store]).fetchall()}
        tops: dict[str, list[str]] = {}
        todo0 = [t for t in dict.fromkeys(terms) if t not in have]
        if todo0:
            for kw, name in con.execute("""SELECT keyword, name FROM search_results WHERE store=? AND rank<=3 AND keyword IN (SELECT unnest(?))
                    QUALIFY dense_rank() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1 ORDER BY keyword, rank""", [store, todo0]).fetchall():
                tops.setdefault(kw, []).append(name)
    finally:
        con.close()
    todo = [t for t in dict.fromkeys(terms) if t not in have]
    ds = DeepSeekClient()
    new: dict[str, dict] = {}
    if todo and ds.available:
        def one(chunk):
            try:
                return ds.label_keywords([{"term": t, "top_apps": tops.get(t, [])} for t in chunk])
            except Exception as e:
                log.warning("label batch failed: %r", e)
                return {}
        with ThreadPoolExecutor(8) as ex:
            for r in ex.map(one, [todo[i:i + 40] for i in range(0, len(todo), 40)]):
                new.update(r)
        now = dt.datetime.now()
        write_retry(lambda c: upsert(c, "keyword_labels", [{"store": store, "keyword": k, "label": v["label"], "p_brand": v["p_brand"],
                                                             "reason": v["reason"], "model": ds.model, "labeled_at": now,
                                                             "specificity": v.get("specificity"),
                                                             "context": "top_apps" if k in tops else "string"}
                                                            for k, v in new.items()]))
    return {t: (have.get(t) or new.get(t) or {}) for t in terms}


def astro_popularity(terms: list[str], store: str = settings.store) -> dict[str, dict]:
    """Popularity + Astro difficulty for any terms (tracked under the research placeholder app). Empty if Astro is down."""
    from ..sources.astro import AstroClient
    from .weekly import astro_up
    if not terms or not astro_up():
        return {}
    out: dict[str, dict] = {}
    with machine_lock("astro"):
        a = AstroClient()
        a.connect()
        a.ensure_placeholder_app(RESEARCH_APP, platform="iphone", store=store)
        for i in range(0, len(terms), 100):
            try:
                a.add_keywords(RESEARCH_APP, terms[i:i + 100], store=store)
            except Exception as e:
                log.warning("astro add_keywords failed: %s", e)
                time.sleep(5)
            time.sleep(1)
        data = a.app_keywords(app_name=RESEARCH_APP)
    want = set(terms)
    for k in data.get("keywords", []):
        t = k["keyword"].strip().lower()
        if t in want:
            out[t] = {"popularity": k.get("popularity"), "difficulty": k.get("difficulty"), "apps_count": k.get("appsCount")}
    d = today()
    write_retry(lambda c: upsert(c, "keyword_metrics", [{"store": store, "keyword": t, "popularity": v["popularity"],
                                                           "difficulty": v["difficulty"], "apps_count": v["apps_count"],
                                                           "source": "astro_tracked", "snapshot_date": d} for t, v in out.items()]))
    return out


def add_to_universe(terms: list[str], source: str, seed: str | None = None, store: str = settings.store) -> int:
    """Make terms part of the scored universe (current keywords snapshot) and remember where they came from."""
    def fn(c):
        snap = c.execute("SELECT max(snapshot_date) FROM keywords WHERE store=?", [store]).fetchone()[0] or today()
        known = {k for (k,) in c.execute("SELECT keyword FROM keywords WHERE store=? AND snapshot_date=?", [store, snap]).fetchall()}
        new = [t for t in dict.fromkeys(terms) if t not in known]
        upsert(c, "keywords", [{"store": store, "keyword": t, "genre": None, "popularity": None, "rank_in_store": None,
                                "snapshot_date": snap} for t in new])
        upsert(c, "longtail_keywords", [{"store": store, "keyword": t, "seed": seed, "source": source, "hint_rank": None,
                                         "popularity": None, "discovered_at": today()} for t in new])
        return len(new)
    return write_retry(fn)


def search_terms(terms: list[str], store: str = settings.store, batch: int = 10,
                 progress: Callable[[int, int], None] | None = None) -> int:
    """iTunes search (top 200) for each term; writes search_results + app profiles every `batch` terms."""
    from ..sources.itunes import ITunesClient, lookup_to_row
    it, d, n, buf_s, buf_a = ITunesClient(), today(), 0, [], []

    def flush():
        nonlocal buf_s, buf_a
        if buf_s:
            s = list({(r["keyword"], r["rank"]): r for r in buf_s}.values())
            a = list({r["app_id"]: r for r in buf_a}.values())   # same app in several results of one batch
            write_retry(lambda c: (upsert(c, "search_results", s), upsert(c, "apps", a)))
        buf_s, buf_a = [], []
    for i, kw in enumerate(terms, 1):
        try:
            res = [r for r in it.search(kw, country=store, limit=200) if r.get("wrapperType") == "software"]
        except Exception as e:
            log.warning("itunes search %r failed: %s", kw, e)
            continue
        buf_s += [{"store": store, "platform": "iphone", "keyword": kw, "rank": j, "app_id": str(r.get("trackId")), "name": r.get("trackName"),
                   "developer": r.get("sellerName") or r.get("artistName"), "genre": r.get("primaryGenreName"),
                   "rating_count": r.get("userRatingCount"), "avg_rating": r.get("averageUserRating"), "subtitle": None,
                   "total_results": len(res), "snapshot_date": d} for j, r in enumerate(res, 1)]
        buf_a += [lookup_to_row(r, store, d) for r in res]
        n += 1
        if i % batch == 0:
            flush()
        if progress:
            progress(i, len(terms))
    flush()
    return n


def mine_pain_points(store: str = settings.store, grades: tuple[str, ...] = ("A", "B"), keywords: list[str] | None = None,
                     per_app_pages: int = 2, max_reviews: int = 80) -> dict:
    """For top niches: fetch recent reviews of the p5-p20 band apps (iTunes RSS, free) and let DeepSeek extract
    recurring, FIXABLE complaints = a feature list for a better newcomer. Writes factor review_gap (+ pain points)."""
    import json
    from concurrent.futures import ThreadPoolExecutor

    from ..metrics.base import ensure_sr_latest
    from ..metrics.sector import _target_cfg, band, sector_estimates
    from ..sources.deepseek import DeepSeekClient
    from ..sources.itunes import ITunesClient
    con = connect_read_live()
    try:
        if keywords is None:
            d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='opp_score' AND entity_type='keyword'").fetchone()[0]
            keywords = [k for k, m in con.execute("SELECT entity_id, meta FROM factor_values WHERE metric='opp_score' AND entity_type='keyword' "
                                                  "AND snapshot_date=? ORDER BY value DESC", [d]).fetchall()
                        if json.loads(m).get("grade") in grades and json.loads(m).get("action") != "No"]
        ensure_sr_latest(con)
        _, (lo, hi), _ = _target_cfg()
        est = sector_estimates(con, store, today())
        band_apps = {kw: [a.app_id for a in band(est.get(kw, []), lo, hi)][:12] for kw in keywords}
        fresh = {r[0] for r in con.execute("SELECT DISTINCT app_id FROM reviews WHERE store=? AND fetched_date>=?",
                                            [store, today() - dt.timedelta(days=14)]).fetchall()}
    finally:
        con.close()
    log.info("pain points: %d niches", len(keywords))
    it, d = ITunesClient(), today()
    todo = [a for a in dict.fromkeys(x for v in band_apps.values() for x in v) if a not in fresh]
    buf = []
    for i, app_id in enumerate(todo, 1):
        try:
            for r in it.reviews(app_id, country=store, pages=per_app_pages):
                upd = r.get("updated")
                try:
                    upd = dt.datetime.fromisoformat(upd).replace(tzinfo=None) if upd else None
                except ValueError:
                    upd = None
                buf.append({"store": store, "app_id": app_id, **{k: r[k] for k in ("review_id", "rating", "title", "body", "version")},
                            "updated": upd, "fetched_date": d})
        except Exception as e:
            log.warning("reviews %s failed: %s", app_id, e)
        if i % 20 == 0 or i == len(todo):
            rows, buf = list({(r["app_id"], r["review_id"]): r for r in buf}.values()), []
            write_retry(lambda c: upsert(c, "reviews", rows))
            log.info("reviews: %d/%d apps", i, len(todo))
    con = connect_read_live()
    try:
        inputs = {}
        for kw, ids in band_apps.items():
            if not ids:
                continue
            revs = con.execute(f"""SELECT rating, title, body FROM reviews WHERE store=? AND app_id IN ({','.join('?' * len(ids))})
                                   AND rating<=3 ORDER BY updated DESC LIMIT ?""", [store, *ids, max_reviews]).fetchall()
            if len(revs) >= 5:
                inputs[kw] = [{"rating": r[0], "title": r[1], "body": (r[2] or "")[:400]} for r in revs]
    finally:
        con.close()
    ds = DeepSeekClient()

    def one(kw):
        try:
            return kw, ds.pain_points(kw, inputs[kw])
        except Exception as e:
            log.warning("pain %s: %s", kw, e)
            return kw, None
    now, rows = dt.datetime.now(), []
    with ThreadPoolExecutor(6) as ex:
        for kw, res in ex.map(one, list(inputs)):
            if res:
                rows.append({"entity_type": "keyword", "entity_id": kw, "metric": "review_gap", "value": float(res.get("opportunity", 0)),
                             "meta": json.dumps({"pain_points": res.get("pain_points", []), "n_reviews": len(inputs[kw])}),
                             "snapshot_date": d, "computed_at": now})
    write_retry(lambda c: (upsert(c, "factor_values", rows),
                           budget.record(c, "deepseek", "llm_pain_points", ds.usage_in + ds.usage_out, ds.cost_usd(), f"{len(rows)} niches")))
    return {"niches": len(keywords), "apps_fetched": len(todo), "with_reviews": len(inputs), "mined": len(rows), "usd": round(ds.cost_usd(), 4)}
