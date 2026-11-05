"""Topic-level social and ad signals for the Opportunities ranking (ScrapeCreators, 1 credit per request).

Per topic: one TikTok search and one YouTube search on the topic label (views = off-store interest; no comments, no
usernames, 90-day retention like the rest of social_posts) and the Meta Ad Library for the topic's top apps by
estimated revenue (an app that keeps paying for ads earns more per user than the ads cost). One summary row per topic
and day goes to `topic_social`; the factors in the scoring model read that table.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics

from ..config import settings
from ..db import connect_read_live, today, upsert
from .reddit_voice import RETENTION_DAYS, _ts
from .social_voice import _iso, _save_rows, _tt_url, collect_ads

log = logging.getLogger(__name__)

TOPIC_SOCIAL_DDL = """CREATE TABLE IF NOT EXISTS topic_social (
  topic_id TEXT, fetched_date DATE, tiktok_videos INTEGER, tiktok_views BIGINT, tiktok_median_views BIGINT,
  youtube_videos INTEGER, youtube_views BIGINT, youtube_median_views BIGINT, apps_checked INTEGER, apps_with_ads INTEGER,
  apps_verified_ads INTEGER, max_active_ads INTEGER, max_oldest_active_days INTEGER, ad_apps TEXT, credits INTEGER,
  PRIMARY KEY (topic_id, fetched_date))"""


class _Partial(Exception):
    """Credit cap hit halfway through a topic's searches: carries what was already fetched."""

    def __init__(self, posts, summary):
        super().__init__("credit cap")
        self.posts, self.summary = posts, summary


def _video_rows(sc, topic_id: str, label: str, d: dt.date) -> tuple[dict[str, dict], dict]:
    """One TikTok + one YouTube search; returns rows for social_posts and a per-platform summary."""
    from ..sources.scrapecreators import CreditCap
    posts: dict[str, dict] = {}
    summary: dict = {}
    for platform in ("tiktok", "youtube"):
        found = []
        try:
            if platform == "tiktok":
                for it in sc.tiktok_search(label).get("search_item_list") or []:
                    v = it.get("aweme_info") or it
                    st = v.get("statistics") or {}
                    if v.get("aweme_id") and not v.get("is_ad"):
                        found.append({"id": "tt_" + str(v["aweme_id"]), "platform": "tiktok", "topic": topic_id, "aspect": "_topic",
                                      "query": label, "title": (v.get("desc") or "")[:300], "views": st.get("play_count"),
                                      "likes": st.get("digg_count"), "comments": st.get("comment_count"),
                                      "created_utc": _ts(v.get("create_time")), "url": _tt_url(v), "fetched_date": d})
            else:
                for v in sc.youtube_search(label).get("videos") or []:
                    if v.get("id"):
                        found.append({"id": "yt_" + str(v["id"]), "platform": "youtube", "topic": topic_id, "aspect": "_topic",
                                      "query": label, "title": (v.get("title") or "")[:300], "views": v.get("viewCountInt"), "likes": None,
                                      "comments": None, "created_utc": _iso(v.get("publishedTime")), "url": v.get("url"), "fetched_date": d})
        except CreditCap:
            raise _Partial(posts, summary) from None
        except Exception as e:
            log.warning("%s search %r: %s", platform, label, e)
        for p in found:
            posts.setdefault(p["id"], p)
        views = [p["views"] or 0 for p in found]
        summary[platform] = {"videos": len(found), "views": sum(views), "median_views": int(statistics.median(views)) if views else None}
    return posts, summary


def _top_apps(con, store: str, keywords: list[str], n: int) -> list[dict]:
    """The topic's biggest apps by estimated revenue: union of its member keywords' sectors."""
    from ..metrics.sector import sector_estimates
    sec = sector_estimates(con, store, today())
    best: dict[str, dict] = {}
    for kw in keywords:
        for a in sec.get(kw, []):
            cur = best.get(a.app_id)
            if cur is None or a.est > cur["est"]:
                best[a.app_id] = {"app_id": a.app_id, "name": a.name, "developer": a.developer, "est": a.est}
    return sorted(best.values(), key=lambda x: -x["est"])[:n]


def collect_topics(topic_ids: list[str], store: str = settings.store, video: bool = True, ads: bool = True,
                   ads_per_topic: int = 5, max_credits: int | None = None) -> dict:
    from .. import budget
    from ..sources.scrapecreators import CreditCap, ScrapeCreators
    from .discover import write_retry
    if not topic_ids:
        return {"topics": [], "credits_used": 0, "usd": 0.0}
    cap = max_credits if max_credits is not None else ((2 if video else 0) + (2 * ads_per_topic if ads else 0)) * len(topic_ids)
    sc = ScrapeCreators(max_credits=cap)
    if not sc.available:
        raise RuntimeError("SCRAPECREATORS_API_KEY missing")
    with connect_read_live() as con:
        ok, why = budget.check(con, "scrapecreators", budget.estimate_job("scrapecreators", "topic_social", cap))
        if not ok:
            raise RuntimeError(f"budget: {why}")
        labels = dict(con.execute("SELECT topic_id, label FROM topics WHERE topic_id IN (SELECT unnest(?))", [topic_ids]).fetchall())
        members: dict[str, list[str]] = {}
        for t, k in con.execute("SELECT topic_id, keyword FROM topic_keywords WHERE store=? AND topic_id IN (SELECT unnest(?))",
                                [store, topic_ids]).fetchall():
            members.setdefault(t, []).append(k)
        apps_of = {t: _top_apps(con, store, members.get(t, []), ads_per_topic) for t in topic_ids} if ads else {}
    write_retry(lambda c: c.execute(TOPIC_SOCIAL_DDL))   # a long-running server may hold an older schema
    d = today()
    out, posts_all = [], {}
    stopped = False
    for t in topic_ids:
        label = labels.get(t)
        if not label:
            log.warning("unknown topic %s", t)
            continue
        before = sc.used
        vs: dict = {}
        ad_res: list[dict] = []
        try:
            if video:
                try:
                    posts, vs = _video_rows(sc, t, label, d)
                except _Partial as p:
                    posts, vs = p.posts, p.summary
                    posts_all.update(posts)
                    raise CreditCap("credit cap reached") from p
                posts_all.update(posts)
            if ads:
                ad_res = collect_ads(sc, apps_of.get(t, []), ads_per_topic)
        except CreditCap as e:          # keep what this topic already paid for, then stop
            log.warning("%s: stopping topic social collection", e)
            stopped = True
            if not vs and not ad_res:       # nothing collected for this topic: no row (a row would read as zero views)
                break
        with_ads = [a for a in ad_res if a.get("active_ads")]
        tt, yt = vs.get("tiktok") or {}, vs.get("youtube") or {}
        out.append({"topic_id": t, "fetched_date": d, "tiktok_videos": tt.get("videos"), "tiktok_views": tt.get("views"),
                    "tiktok_median_views": tt.get("median_views"), "youtube_videos": yt.get("videos"), "youtube_views": yt.get("views"),
                    "youtube_median_views": yt.get("median_views"), "apps_checked": len(ad_res),
                    "apps_with_ads": len(with_ads), "apps_verified_ads": sum(1 for a in with_ads if a.get("verified")),
                    "max_active_ads": max((a["active_ads"] for a in with_ads), default=0),
                    "max_oldest_active_days": max((a.get("oldest_active_days") or 0 for a in with_ads), default=None) if with_ads else None,
                    "ad_apps": json.dumps([{k: a.get(k) for k in ("app_id", "name", "active_ads", "oldest_active_days", "verified")} for a in ad_res]),
                    "credits": sc.used - before, "label": label})
        if stopped:
            break
    rows = [{k: v for k, v in r.items() if k != "label"} for r in out]
    _save_rows(lambda c: (c.execute("DELETE FROM social_posts WHERE fetched_date < ?", [d - dt.timedelta(days=RETENTION_DAYS)]),
                           upsert(c, "social_posts", list(posts_all.values())), upsert(c, "topic_social", rows),
                           budget.record(c, "scrapecreators", "topic_social", sc.used, sc.cost_usd(), f"{len(rows)} topics")))
    return {"topics": out, "credits_used": sc.used, "usd": sc.cost_usd(), "credits_remaining": sc.remaining}


def topics_by_grade(con, grades: str = "AB", limit: int = 100) -> list[str]:
    """Topic ids whose latest opportunity grade is in `grades`, best score first."""
    d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE entity_type='topic' AND metric='opp_score'").fetchone()[0]
    if d is None:
        return []
    rows = con.execute("""SELECT entity_id FROM factor_values WHERE entity_type='topic' AND metric='opp_score' AND snapshot_date=?
                          AND json_extract_string(meta, '$.grade') IN (SELECT unnest(?)) ORDER BY value DESC LIMIT ?""",
                       [d, list(grades.upper()), limit]).fetchall()
    return [r[0] for r in rows]
