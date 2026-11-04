"""Grounded aspects: collect real evidence about a topic first, then let DeepSeek only GROUP it into aspects.

Evidence (each aspect shows how much of each it rests on):
  S  App Store autocomplete for the topic (+ a-z) and for alternative words Apple confirms, plus searches we already
     track that contain them; popularity from our data or Astro -> searches/day
  A  competitor apps (iTunes search: name + start of description)
  R  4-5 star reviews of the biggest competitors (stored ones first, a few fetched)
  P  Reddit post titles (ScrapeCreators, a few credits; stored with the 90-day retention, no usernames)
Aspects DeepSeek adds without evidence are kept but marked idea_only (unticked in the dashboard).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import string
from collections.abc import Callable

from ..config import settings
from ..db import connect_read_live, today, upsert
from .collect import is_latin
from .discover import astro_popularity, looks_like_app_name, write_retry
from .index import searches_per_day

log = logging.getLogger(__name__)

MAX_SEARCHES, MAX_APPS, MAX_REVIEWS, REVIEW_APPS_FETCH = 250, 40, 120, 6


def _latest_pop(con, terms: list[str]) -> dict[str, int]:
    pop = {}
    for table in ("keywords", "keyword_metrics"):
        for k, p in con.execute(f"""SELECT keyword, popularity FROM {table} WHERE popularity IS NOT NULL AND keyword IN (SELECT unnest(?))
                QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [terms]).fetchall():
            pop.setdefault(k, p)
    return pop


def ground_aspects(topic: str, app: str | None = None, store: str = settings.store, reddit_credits: int = 4,
                   progress: Callable[[str, int], None] | None = None) -> dict:
    from .. import budget
    from ..sources.deepseek import DeepSeekClient
    from ..sources.itunes import ITunesClient
    step = progress or (lambda name, pct: log.info("%s %d%%", name, pct))
    topic = topic.strip().lower()
    ds, it = DeepSeekClient(), ITunesClient()
    notes: list[str] = []

    # 1. words people type for the same thing (only kept if Apple autocompletes them)
    step("alternative words", 3)
    try:
        roots = [r for r in ds.search_roots(topic) if r != topic]
    except Exception as e:
        roots, _ = [], notes.append(f"alternative words skipped: {e}")

    # 2. App Store autocomplete
    probes = [topic] + [f"{topic} {c}" for c in string.ascii_lowercase] + roots + [f"{r} " for r in roots]
    sugg: dict[str, str] = {}
    live_roots = set()
    for i, q in enumerate(probes, 1):
        try:
            for h in it.hints(q):
                if is_latin(h) and not looks_like_app_name(h):
                    sugg.setdefault(h, q.strip())
                    live_roots |= {r for r in roots if r in h}
        except Exception as e:
            log.warning("hints %r: %s", q, e)
        if i % 4 == 0:
            step("App Store autocomplete", 5 + int(40 * i / len(probes)))
    dead = [r for r in roots if r not in live_roots]
    if dead:
        notes.append("dropped words Apple does not complete: " + ", ".join(dead))
    words = [topic, *sorted(live_roots)]

    # 3. searches we already track that contain the topic words + popularity
    step("searches we already track", 47)
    with connect_read_live() as con:
        like = " OR ".join(["keyword LIKE ?"] * len(words))
        known = [k for (k,) in con.execute(f"""SELECT DISTINCT keyword FROM (SELECT keyword FROM keywords UNION SELECT keyword FROM keyword_metrics)
                 WHERE {like}""", [f"%{w}%" for w in words]).fetchall() if is_latin(k)]
        terms = list(dict.fromkeys(list(sugg) + known))
        pop = _latest_pop(con, terms)
    missing = [t for t in terms if t not in pop][:200]
    if missing:
        step("popularity (Astro)", 52)
        ast = astro_popularity(missing, store)
        pop.update({k: v["popularity"] for k, v in ast.items() if v.get("popularity") is not None})
        if not ast:
            notes.append(f"Astro not running: popularity unknown for {len(missing)} searches")
    terms = sorted(terms, key=lambda t: -(pop.get(t) or 0))[:MAX_SEARCHES]
    S = {f"S{i}": t for i, t in enumerate(terms, 1)}

    # 4. competitors
    step("competitor apps", 60)
    apps: dict[str, dict] = {}
    for q in [topic, *sorted(live_roots)[:2]]:
        try:
            for r in it.search(q, limit=50 if q == topic else 25):
                apps.setdefault(str(r["trackId"]), r)
        except Exception as e:
            log.warning("search %r: %s", q, e)
    top_apps = sorted(apps.values(), key=lambda r: -(r.get("userRatingCount") or 0))[:MAX_APPS]
    A = {f"A{i}": r for i, r in enumerate(top_apps, 1)}

    # 5. happy reviews of the biggest competitors (stored first)
    step("4-5 star reviews", 68)
    ids = [str(r["trackId"]) for r in top_apps]
    with connect_read_live() as con:
        revs = con.execute("""SELECT app_id, title, body FROM reviews WHERE store=? AND app_id IN (SELECT unnest(?)) AND rating >= 4
                              AND length(body) >= 40 ORDER BY updated DESC""", [store, ids]).fetchall()
    have = {a for a, _, _ in revs}
    fetched = []
    for aid in [a for a in ids if a not in have][:REVIEW_APPS_FETCH]:
        try:
            for rv in it.reviews(aid, pages=1):
                fetched.append({"store": store, "app_id": aid, **rv, "updated": rv.get("updated"), "fetched_date": today()})
                if rv["rating"] >= 4 and len(rv.get("body") or "") >= 40:
                    revs.append((aid, rv.get("title"), rv["body"]))
        except Exception as e:
            log.warning("reviews %s: %s", aid, e)
    if fetched:
        for f in fetched:
            try:
                f["updated"] = dt.datetime.fromisoformat(f["updated"]).replace(tzinfo=None) if f["updated"] else None
            except ValueError:
                f["updated"] = None
        write_retry(lambda c: upsert(c, "reviews", fetched))
    per_app: dict[str, int] = {}
    picked = []
    for aid, title, body in revs:          # spread over apps so one giant does not fill the list
        if per_app.get(aid, 0) < 12 and len(picked) < MAX_REVIEWS:
            per_app[aid] = per_app.get(aid, 0) + 1
            picked.append(f"{title or ''}: {body}"[:240])
    R = {f"R{i}": t for i, t in enumerate(picked, 1)}

    # 6. Reddit titles (a few credits)
    P: dict[str, dict] = {}
    used, usd = 0, 0.0
    if reddit_credits > 0:
        step("Reddit titles", 76)
        try:
            P, used, usd = _reddit_titles(topic, store, reddit_credits)
        except Exception as e:
            notes.append(f"Reddit skipped: {e}")

    # 7. DeepSeek groups the evidence
    step("grouping the evidence (DeepSeek)", 82)
    ev = {"searches": [[k, t, round(searches_per_day(pop.get(t)))] for k, t in S.items()],
          "apps": [[k, r.get("trackName"), (r.get("description") or "")[:220].replace("\n", " ")] for k, r in A.items()],
          "reviews": [[k, t] for k, t in R.items()],
          "reddit": [[k, p["title"][:160]] for k, p in P.items()]}
    raw = ds.grounded_aspects(topic, app, ev)
    write_retry(lambda c: (budget.record(c, "deepseek", "grounded_aspects", ds.usage_in + ds.usage_out, ds.cost_usd(), topic),
                           budget.record(c, "scrapecreators", "aspect_evidence", used, usd, topic) if used else None), tries=3, wait=1)

    out = []
    for a in raw:
        cited = [e for e in dict.fromkeys(a["evidence"]) if e in S or e in A or e in R or e in P]
        s = [S[e] for e in cited if e in S]
        ap = [A[e] for e in cited if e in A]
        rv = [R[e] for e in cited if e in R]
        rd = [P[e] for e in cited if e in P]
        spd = sum(searches_per_day(pop.get(t)) for t in s)
        seeds = list(dict.fromkeys([x for x in a["seeds"] if x in S.values()] + sorted(s, key=lambda t: -(pop.get(t) or 0)) + a["seeds"]))[:6]
        kinds = sum(1 for x in (s, ap, rv, rd) if x)
        out.append({"name": a["name"], "description": a["description"], "audience": a["audience"], "seeds": seeds,
                    "terms": s, "idea_only": not cited,
                    "evidence": {"searches": len(s), "searches_per_day": round(spd), "known_popularity": sum(1 for t in s if pop.get(t)),
                                 "top_searches": sorted(s, key=lambda t: -(pop.get(t) or 0))[:4],
                                 "apps": len(ap), "top_apps": [x.get("trackName") for x in sorted(ap, key=lambda r: -(r.get("userRatingCount") or 0))[:3]],
                                 "reviews": len(rv), "review_quote": rv[0][:160] if rv else None,
                                 "reddit": len(rd), "reddit_title": max(rd, key=lambda p: p.get("score") or 0)["title"][:140] if rd else None,
                                 "kinds": kinds},
                    "strength": round(math.log10(1 + spd) * 2 + math.log1p(len(rv)) + math.log1p(len(rd)) + 0.5 * kinds + (0 if cited else -99), 2)})
    out.sort(key=lambda a: -a["strength"])
    covered = {t for a in out for t in a["terms"]}
    step("done", 100)
    return {"topic": topic, "aspects": out, "grounded": True, "roots": sorted(live_roots), "notes": notes,
            "evidence_counts": {"searches": len(S), "searches_grouped": len(covered), "apps": len(A), "reviews": len(R),
                                "reddit": len(P), "reviews_fetched_apps": len({f["app_id"] for f in fetched})},
            "cost": {"deepseek_usd": round(ds.cost_usd(), 4), "reddit_credits": used, "reddit_usd": round(usd, 4)}}


def _reddit_titles(topic: str, store: str, credits: int) -> tuple[dict, int, float]:
    from ..sources.scrapecreators import CreditCap, ScrapeCreators
    from .reddit_voice import _ts
    sc = ScrapeCreators(max_credits=credits)
    if not sc.available:
        raise RuntimeError("SCRAPECREATORS_API_KEY missing")
    posts: dict[str, dict] = {}
    d = today()
    try:
        for q, sort in [(topic, "top"), (topic, "relevance"), (f"{topic} app", "relevance"), (f"{topic} help", "relevance")][:credits]:
            for p in sc.search(q, sort=sort).get("posts") or []:
                pid = p.get("id") or p.get("name")
                if pid and pid not in posts and p.get("title"):
                    posts[pid] = {"id": pid, "topic": topic, "aspect": "_discovery", "query": q, "subreddit": p.get("subreddit"),
                                  "title": p["title"][:300], "body": (p.get("selftext") or "")[:3000], "score": p.get("score"),
                                  "num_comments": p.get("num_comments"), "created_utc": _ts(p.get("created_utc")),
                                  "permalink": p.get("permalink") or (f"/r/{p['subreddit']}/comments/{str(pid).removeprefix('t3_')}/" if p.get("subreddit") else None),
                                  "fetched_date": d}
    except CreditCap as e:
        log.warning("reddit titles: %s", e)
    if posts:
        write_retry(lambda c: upsert(c, "reddit_posts", list(posts.values())))
    top = sorted(posts.values(), key=lambda p: -((p["score"] or 0) + 3 * (p["num_comments"] or 0)))[:80]
    return {f"P{i}": p for i, p in enumerate(top, 1)}, sc.used, sc.cost_usd()
