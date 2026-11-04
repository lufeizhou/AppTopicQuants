"""The weekly full run (and the daily light run) with the pre-run spend plan. Every source runs only if its credentials
exist; paid jobs are guarded by budget caps; the run ends with scoring and a read-only
snapshot copy for the dashboard."""
from __future__ import annotations

import datetime as dt
import logging

from .. import budget
from ..config import settings
from ..db import snapshot_copy
from ..sources.appleads import AppleAdsClient
from ..sources.asc import ASCClient
from ..sources.astro import AstroClient
from ..sources.dataforseo import DataForSEOClient
from ..sources.deepseek import DeepSeekClient
from . import collect as c
from .score import score_all

log = logging.getLogger("apptopics.run")


def _stale(con, table: str, days: int, date_col: str = "snapshot_date") -> bool:
    d = con.execute(f"SELECT max({date_col}) FROM {table}").fetchone()[0]
    return d is None or (dt.date.today() - d).days >= days


def _topics_due(con) -> bool:
    return dt.date.today().weekday() == 0 or not con.execute("SELECT count(*) FROM topics").fetchone()[0]


def astro_up() -> bool:
    try:
        AstroClient(timeout=5).connect()
        return True
    except Exception:
        return False


def search_backend() -> str:
    """auto: dataforseo if configured, else astro if its app is running, else the free iTunes Search API."""
    b = settings.weights().get("sources", {}).get("search_backend", "auto")
    if b != "auto":
        return b
    if DataForSEOClient().available:
        return "dataforseo"
    return "astro" if astro_up() else "itunes"


def run_plan(con, store: str = settings.store) -> list[dict]:
    """Estimated paid calls for the next full run, given credentials and cadence (no calls are made).
    APIs without a key are listed too (key_missing=True, not counted in the total) so the user
    sees what adding the key would cost."""
    cad = budget.load().get("cadence", {})
    src = settings.weights().get("sources", {})
    have = {"deepseek": DeepSeekClient().available, "dataforseo": DataForSEOClient().available}
    counts: dict[tuple[str, str], int] = {}
    n = min(1500, con.execute("SELECT count(*) FROM keywords WHERE store=?", [store]).fetchone()[0] or 1500)
    counts[("deepseek", "llm_brands")] = n
    counts[("deepseek", "llm_buildability")] = n
    if _topics_due(con):
        counts[("deepseek", "topics")] = n
    if src.get("search_backend", "auto") in ("auto", "dataforseo"):
        counts[("dataforseo", "search")] = src.get("search_top", 1000)
    if _stale(con, "keyword_volume", cad.get("volume_every_days", 7)):
        counts[("dataforseo", "volume")] = 1500
    if _stale(con, "trends", cad.get("trends_every_days", 7)):
        counts[("dataforseo", "trends")] = 1000
    active = {k: v for k, v in counts.items() if have[k[0]]}
    rows = budget.plan(con, active)
    for (api, job), items in counts.items():
        if not have[api]:
            rows.insert(-1, {"api": api, "job": job, "items": items, "est_usd": round(budget.estimate_job(api, job, items), 3),
                             "mtd_usd": 0.0, "cap_usd": budget.load().get("caps", {}).get(api), "ok": True,
                             "key_missing": True, "reason": f"no {api} key in .env - skipped; this is what it would cost"})
    return rows


def run_full(con, store: str = settings.store, top: int = 300, play_top: int = 150, reviews_top: int = 150,
                social_top: int = 300, play: bool = True, difficulty: bool = True, llm: bool = True, external: bool = True) -> dict:
    budget.ensure_schema(con)
    cad = budget.load().get("cadence", {})
    src = settings.weights().get("sources", {})
    for r in run_plan(con, store):
        if not r["ok"] and r["api"] != "total":
            budget.warn(con, "plan", r["api"], f"this run's {r['job']} (est ${r['est_usd']:.2f}) would exceed the cap: {r['reason']}")

    # 1. universe and charts
    if AppleAdsClient().available:
        c.collect_popularity(con, store, weeks=8)
        c.keywords_from_popularity(con, store)
    elif _stale(con, "keywords", cad.get("keywords_every_days", 7)) and astro_up():
        c.collect_keywords(con, store)
    c.collect_charts(con, store)

    # 2. sectors: search pages, metadata, difficulty, Play
    backend = search_backend()
    n_search = src.get("search_top", 1000) if backend != "astro" else top
    kws = c.selected(con, store, n_search)
    c.collect_search_auto(con, kws, store, backend)
    kws = kws[:top]
    c.collect_apps(con, store)
    if difficulty and astro_up():  # optional: Astro's own difficulty is blended into ours when tracked
        c.collect_difficulty(con, kws, store)
    if play:
        c.collect_play(con, kws[:play_top], store)
        c.collect_play_charts(con, store)

    # 3. LLM classification before the first scoring (brands/buildability shape the ranking)
    if llm and DeepSeekClient().available:
        c.collect_llm_brands(con, c.selected(con, store, 1500, brand_filter=False), store)
        c.collect_llm_buildability(con, c.selected(con, store, 1500), store)
        if _topics_due(con):
            c.collect_topics(con, c.selected(con, store, 1500), store)
    first = score_all(con, store)
    log.info("first scoring: %s", first)

    # 4. ranking-dependent sources
    ranked = c.ranked(con, max(reviews_top, social_top))
    if external:
        if DataForSEOClient().available:
            if _stale(con, "keyword_volume", cad.get("volume_every_days", 7)):
                c.collect_volume(con, c.selected(con, store, 1500), store)
            if _stale(con, "trends", cad.get("trends_every_days", 7)):
                c.collect_trends(con, c.ranked(con, 1000), store)
        c.collect_reviews(con, c.band_app_ids(con, store, ranked[:reviews_top]), store)
    if ASCClient().available:
        c.collect_asc(con, 90)

    # 5. final scoring + read-only copy for the dashboard
    final = score_all(con, store)
    final["read_copy"] = str(snapshot_copy(con))
    return final


def run_light(con, store: str = settings.store, top: int = 1500) -> dict:
    """Daily, free, a few minutes: Apple charts (grossing ranks move daily) and fresh rating counts for the band apps
    of the top keywords, so rating velocity becomes a true day-over-day series (the `observed` estimator and the
    backtest's young-app outcomes both need it). Own-app pages are refreshed by the CLI before this (lock-free)."""
    n_charts = c.collect_charts(con, store)
    ids = c.band_app_ids(con, store, c.ranked(con, top))
    n_apps = c.collect_apps_for(con, ids, store)
    out = {"charts": n_charts, "band_apps_refreshed": n_apps, "read_copy": str(snapshot_copy(con))}
    log.info("light run: %s", out)
    return out
