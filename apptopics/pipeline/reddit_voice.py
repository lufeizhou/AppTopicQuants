"""Add Reddit (via ScrapeCreators) to a finished research run, without searching the App Store again.

Per aspect: post searches (past year) -> comments of the most-discussed posts -> details of the top subreddits.
Stops at the credit cap. Stores posts/comments WITHOUT usernames for 90 days. DeepSeek then merges App Store
4-5 star findings and Reddit into one "voice of users", and the positioning options are regenerated with it."""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter

from ..db import today, upsert
from .discover import write_retry
from .research import _save, load_run

log = logging.getLogger("apptopics.reddit")
RETENTION_DAYS = 90


def _ts(x) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(float(x), dt.UTC).replace(tzinfo=None) if x is not None else None
    except (TypeError, ValueError):
        return None


def _flatten(comments: list[dict], depth: int = 0, out: list | None = None) -> list[dict]:
    out = [] if out is None else out
    for c in comments or []:
        if c.get("body") and c.get("body") not in ("[deleted]", "[removed]"):
            out.append(c)
        rep = c.get("replies")
        if depth < 2 and isinstance(rep, dict):
            _flatten(rep.get("items") or [], depth + 1, out)
    return out


def add_reddit(run_id: str, max_credits: int = 100, posts_for_comments: int = 10, video: bool = True, ads: bool = True,
               video_per_aspect: int = 8, ad_apps: int = 12) -> dict:
    from .. import budget
    from ..sources.deepseek import DeepSeekClient
    from ..sources.scrapecreators import CreditCap, ScrapeCreators
    run = load_run(run_id)
    if not run or not (run.get("result") or {}).get("market"):
        raise ValueError(f"run {run_id} has no market report")
    topic, m = run["topic"], run["result"]["market"]
    aspects = run.get("aspects") or [{"name": topic, "seeds": [topic]}]
    sc = ScrapeCreators(max_credits=max_credits)
    if not sc.available:
        raise RuntimeError("SCRAPECREATORS_API_KEY missing in .env")
    write_retry(lambda c: (c.execute("DELETE FROM reddit_posts WHERE fetched_date < ?", [today() - dt.timedelta(days=RETENTION_DAYS)]),
                           c.execute("DELETE FROM reddit_comments WHERE fetched_date < ?", [today() - dt.timedelta(days=RETENTION_DAYS)])))
    d, posts, comments, boxes = today(), {}, {}, []
    from ..db import connect_read_live
    stored: dict[str, list[dict]] = {}
    with connect_read_live() as con:
        cols = ["id", "topic", "aspect", "query", "subreddit", "title", "body", "score", "num_comments", "created_utc", "permalink", "fetched_date"]
        for row in con.execute(f"SELECT {','.join(cols)} FROM reddit_posts WHERE topic=?", [topic]).fetchall():
            p = dict(zip(cols, row))
            if not p["permalink"] and p["subreddit"]:
                p["permalink"] = f"/r/{p['subreddit']}/comments/{str(p['id']).removeprefix('t3_')}/"
            stored.setdefault(p["aspect"], []).append(p)
        ccols = ["id", "post_id", "topic", "aspect", "subreddit", "body", "score", "created_utc", "permalink", "fetched_date"]
        stored_c: dict[str, list[dict]] = {}
        for row in con.execute(f"SELECT {','.join(ccols)} FROM reddit_comments WHERE topic=?", [topic]).fetchall():
            c = dict(zip(ccols, row))
            stored_c.setdefault(c["post_id"], []).append(c)
    # spread the credits over the aspects that still need a search (stored ones only pay for comments)
    todo = [a for a in aspects if not stored.get(a["name"])]
    per = max(6, max_credits // max(1, len(aspects)))
    n_q, n_sub = (3, 3) if per >= 16 else (2, 2) if per >= 9 else (1, 1)
    posts_for_comments = min(posts_for_comments, max(2, per - (n_q if todo else 0) - n_sub))
    try:
        for a in aspects:
            name = a["name"]
            words = " ".join(w for w in name.lower().replace("and", " ").split() if len(w) > 2)
            queries = list(dict.fromkeys([f"{topic} {words}", *[s for s in a.get("seeds", [])][:2]]))[:n_q]
            for p in stored.get(name, []):      # posts fetched earlier for this run: no need to pay for the search again
                posts.setdefault(p["id"], p)
            for q in ([] if stored.get(name) else queries):
                try:
                    r = sc.search(q)
                except CreditCap:
                    raise
                except Exception as e:          # one failed request must not drop everything already paid for
                    log.warning("reddit search %r: %s", q, e)
                    continue
                for p in r.get("posts") or []:
                    pid = p.get("id") or p.get("name")
                    if pid and p.get("subreddit") and not p.get("permalink"):   # compact responses omit the link
                        p["permalink"] = f"/r/{p['subreddit']}/comments/{str(pid).removeprefix('t3_')}/"
                    if pid and pid not in posts:
                        posts[pid] = {"id": pid, "topic": topic, "aspect": name, "query": q, "subreddit": p.get("subreddit"),
                                      "title": (p.get("title") or "")[:300], "body": (p.get("selftext") or "")[:3000],
                                      "score": p.get("score"), "num_comments": p.get("num_comments"),
                                      "created_utc": _ts(p.get("created_utc")), "permalink": p.get("permalink"), "fetched_date": d}
            mine = sorted((p for p in posts.values() if p["aspect"] == name), key=lambda p: -(p["num_comments"] or 0))
            for p in mine[:posts_for_comments]:
                if stored_c.get(p["id"]):           # comments read earlier: reuse instead of paying again
                    for c in stored_c[p["id"]]:
                        comments[c["id"]] = {**c, "aspect": name}
                    continue
                if not p["permalink"]:
                    continue
                try:
                    r = sc.comments("https://www.reddit.com" + p["permalink"])
                except CreditCap:
                    raise
                except Exception as e:
                    log.warning("reddit comments %s: %s", p["permalink"], e)
                    continue
                flat = sorted(_flatten(r.get("comments") or []), key=lambda c: -(c.get("score") or 0))[:15]
                for c in flat:
                    cid = c.get("id") or c.get("name")
                    if cid:
                        comments[cid] = {"id": cid, "post_id": p["id"], "topic": topic, "aspect": name, "subreddit": p["subreddit"],
                                         "body": (c.get("body") or "")[:2000], "score": c.get("score"),
                                         "created_utc": _ts(c.get("created_utc")), "permalink": c.get("permalink"), "fetched_date": d}
            subs = Counter(p["subreddit"] for p in mine if p["subreddit"])
            top_subs = []
            for sub, n in subs.most_common(n_sub):
                det = {}
                try:
                    det = sc.subreddit(sub)
                except CreditCap:
                    raise
                except Exception as e:
                    log.warning("subreddit %s: %s", sub, e)
                top_subs.append({"subreddit": sub, "posts": n, "weekly_active_users": det.get("weekly_active_users"),
                                 "weekly_contributions": det.get("weekly_contributions")})
            now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            recent = [p for p in mine if p["created_utc"]]
            boxes.append({"aspect": name, "posts": len(mine), "comments": sum(1 for c in comments.values() if c["aspect"] == name),
                          "posts_last_90d": sum(1 for p in recent if (now - p["created_utc"]).days <= 90),
                          "median_score": sorted(p["score"] or 0 for p in mine)[len(mine) // 2] if mine else None,
                          "top_subreddits": top_subs, "queries": queries})
    except CreditCap as e:
        log.warning("%s: stopping Reddit collection", e)
    write_retry(lambda c: (upsert(c, "reddit_posts", list(posts.values())), upsert(c, "reddit_comments", list(comments.values())),
                           budget.record(c, "scrapecreators", "reddit_research", sc.used, sc.cost_usd(), f"{topic}: {len(posts)} posts")))
    # TikTok + YouTube per aspect, Meta ads of the top competitors (own credit budget, on top of Reddit)
    social_boxes, ads_out, sc2 = [], [], None
    vposts, vcomments = {}, {}
    if video or ads:
        from .social_voice import collect_ads, collect_video
        sc2 = ScrapeCreators(max_credits=(video_per_aspect * len(aspects) if video else 0) + (2 * ad_apps if ads else 0))
        if video:
            vposts, vcomments, social_boxes = collect_video(sc2, topic, aspects)
        if ads:
            ads_out = collect_ads(sc2, m.get("competitors") or [], ad_apps)
        write_retry(lambda c: budget.record(c, "scrapecreators", "video_and_ads", sc2.used, sc2.cost_usd(),
                                            f"{topic}: {len(vposts)} videos, {len(ads_out)} advertisers checked"))
    # merge with the App Store findings
    ds = DeepSeekClient()
    reddit_in = {}
    for a in aspects:
        ps = sorted((p for p in posts.values() if p["aspect"] == a["name"]), key=lambda p: -((p["score"] or 0) + 3 * (p["num_comments"] or 0)))[:40]
        cs = sorted((c for c in comments.values() if c["aspect"] == a["name"]), key=lambda c: -(c["score"] or 0))[:80]
        reddit_in[a["name"]] = {"posts": [{"sub": p["subreddit"], "title": p["title"], "text": p["body"][:400], "score": p["score"], "comments": p["num_comments"]} for p in ps],
                                "comments": [{"sub": c["subreddit"], "text": c["body"][:300], "score": c["score"]} for c in cs]}
        for plat in ("tiktok", "youtube"):
            vs = sorted((p for p in vposts.values() if p["aspect"] == a["name"] and p["platform"] == plat), key=lambda p: -(p["views"] or 0))[:15]
            vc = sorted((c for c in vcomments.values() if c["aspect"] == a["name"] and c["platform"] == plat), key=lambda c: -(c["likes"] or 0))[:40]
            reddit_in[a["name"]][plat] = {"videos": [{"title": p["title"][:200], "views": p["views"]} for p in vs],
                                          "comments": [{"text": c["body"][:300], "likes": c["likes"]} for c in vc]}
    voice = ds.voice_of_users(topic, [a["name"] for a in aspects], {k: m.get("praise", {}).get(k) for k in ("pains_solved", "loved_features", "wishes", "audiences")}, reddit_in)
    voice["n_reddit_posts"], voice["n_reddit_comments"], voice["n_app_reviews"] = len(posts), len(comments), (m.get("praise") or {}).get("n_reviews")
    voice["n_videos"], voice["n_video_comments"] = len(vposts), len(vcomments)
    m["voice"] = voice
    m["reddit"] = {"aspects": boxes, "credits_used": sc.used, "credits_remaining": (sc2.remaining if sc2 and sc2.remaining is not None else sc.remaining),
                   "usd": sc.cost_usd(), "fetched": d.isoformat()}
    if sc2:
        m["social"] = {"aspects": social_boxes, "credits_used": sc2.used, "usd": sc2.cost_usd(), "fetched": d.isoformat()}
        if ads:
            m["ads"] = ads_out
    _highlights(run, ds)
    write_retry(lambda c: budget.record(c, "deepseek", "voice_of_users", ds.usage_in + ds.usage_out, ds.cost_usd(), topic))
    _save(run)
    return {"posts": len(posts), "comments": len(comments), "videos": len(vposts), "video_comments": len(vcomments),
            "advertisers": sum(1 for a in ads_out if a.get("active_ads")), "credits_used": sc.used + (sc2.used if sc2 else 0), "credits_remaining": sc.remaining, "usd": round(sc.cost_usd() + (sc2.cost_usd() if sc2 else 0), 4)}


def _highlights(run: dict, ds) -> bool:
    """Regenerate the positioning options from the market facts + voice of users. Two tries (DeepSeek JSON can break)."""
    m, topic = run["result"]["market"], run["topic"]
    voice = m.get("voice") or {}
    ctx = {"topic": topic, "our_app": run.get("app") or "",
           "aspects": [{k: r.get(k) for k in ("aspect", "band", "searches_day", "established", "giants", "avg_weak_spots",
                                               "newcomer_share", "small_apps_earn", "best_search", "best_grade")} for r in m.get("aspects", [])],
           "features": [{"name": f["name"], "coverage": f.get("coverage")} for f in m.get("features", [])],
           "voice_of_users": {k: voice.get(k) for k in ("pains", "wishes", "audiences", "loved", "phrases")},
           "prices": m.get("prices"), "competitor_names": [c["name"] for c in m.get("competitors", [])],
           "competitor_meta_ads": [{k: a.get(k) for k in ("name", "active_ads", "oldest_active_days", "messages")}
                                   for a in (m.get("ads") or []) if a.get("active_ads")]}
    for attempt in range(2):
        try:
            opts = ds.highlights(ctx)
            if opts:
                m["highlights"] = opts
                return True
        except Exception as e:
            log.warning("highlights attempt %d failed: %s", attempt + 1, e)
    return False
