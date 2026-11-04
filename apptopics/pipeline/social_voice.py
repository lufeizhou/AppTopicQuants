"""TikTok + YouTube (what people watch and say) and Meta Ad Library (which competitors buy ads) for a research run.

All through ScrapeCreators (1 credit per request). No usernames or channel names of commenters are stored; posts and
comments follow the same 90-day retention as Reddit. Ads are public advertiser data (page, text, dates).
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from ..db import today, upsert
from .discover import write_retry
from .reddit_voice import RETENTION_DAYS, _ts

log = logging.getLogger(__name__)


def _save_rows(fn) -> None:
    """Store, but never lose paid-for results over a storage problem: the report still uses them from memory."""
    try:
        write_retry(fn)
    except Exception as e:
        log.warning("could not store social data (kept in the report): %s", e)


def _iso(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None) if s else None
    except ValueError:
        return None


def _tt_url(v: dict) -> str | None:
    """TikTok link without the creator's handle (TikTok resolves any handle for a video id); no usernames stored."""
    vid = v.get("aweme_id") or v.get("id")
    return f"https://www.tiktok.com/@_/video/{vid}" if vid else (v.get("url") or v.get("share_url"))


def collect_video(sc, topic: str, aspects: list[dict], videos_for_comments: int = 3) -> tuple[dict, dict, list[dict]]:
    """Per aspect: 1 TikTok search + comments on the top videos, 1 YouTube search + comments on the top videos."""
    from ..sources.scrapecreators import CreditCap
    d, posts, comments, boxes = today(), {}, {}, []
    try:
        for a in aspects:
            name = a["name"]
            words = " ".join(w for w in name.lower().replace(" and ", " ").replace(",", " ").split() if len(w) > 2 and w not in topic)
            q = f"{topic} {words}".strip()
            box = {"aspect": name, "query": q}
            for platform in ("tiktok", "youtube"):
                found = []
                try:
                    if platform == "tiktok":
                        for it in sc.tiktok_search(q).get("search_item_list") or []:
                            v = it.get("aweme_info") or it
                            st = v.get("statistics") or {}
                            if v.get("aweme_id") and not v.get("is_ad"):
                                found.append({"id": "tt_" + str(v["aweme_id"]), "platform": "tiktok", "topic": topic, "aspect": name, "query": q,
                                              "title": (v.get("desc") or "")[:300], "views": st.get("play_count"), "likes": st.get("digg_count"),
                                              "comments": st.get("comment_count"), "created_utc": _ts(v.get("create_time")),
                                              "url": _tt_url(v), "_api_url": v.get("url") or v.get("share_url"), "fetched_date": d})
                    else:
                        for v in sc.youtube_search(q).get("videos") or []:
                            if v.get("id"):
                                found.append({"id": "yt_" + str(v["id"]), "platform": "youtube", "topic": topic, "aspect": name, "query": q,
                                              "title": (v.get("title") or "")[:300], "views": v.get("viewCountInt"), "likes": None,
                                              "comments": None, "created_utc": _iso(v.get("publishedTime")), "url": v.get("url"), "fetched_date": d})
                except CreditCap:
                    raise
                except Exception as e:
                    log.warning("%s search %r: %s", platform, q, e)
                    box[platform] = {"videos": None, "views": None, "median_views": None, "comments": 0, "top": None, "error": "search failed"}
                    continue
                for p in found:
                    posts.setdefault(p["id"], p)
                # comments on the most-watched videos that have a discussion
                top = sorted(found, key=lambda p: -((p["comments"] or 0) * 20 + (p["views"] or 0)))[:videos_for_comments]
                n_c = 0
                for p in top:
                    if not p["url"]:
                        continue
                    try:
                        if platform == "tiktok":
                            for c in sc.tiktok_comments(p.get("_api_url") or p["url"]).get("comments") or []:
                                if c.get("text"):
                                    cid = "tt_" + str(c.get("cid") or f"{p['id']}_{n_c}")
                                    comments[cid] = {"id": cid, "platform": "tiktok", "post_id": p["id"], "topic": topic, "aspect": name,
                                                     "body": c["text"][:1000], "likes": c.get("digg_count"),
                                                     "created_utc": _ts(c.get("create_time")), "fetched_date": d}
                                    n_c += 1
                        else:
                            for c in sc.youtube_comments(p["url"]).get("comments") or []:
                                if c.get("content"):
                                    cid = "yt_" + str(c.get("id") or f"{p['id']}_{n_c}")
                                    comments[cid] = {"id": cid, "platform": "youtube", "post_id": p["id"], "topic": topic, "aspect": name,
                                                     "body": c["content"][:1000], "likes": (c.get("engagement") or {}).get("likes"),
                                                     "created_utc": _iso(c.get("publishedTime")), "fetched_date": d}
                                    n_c += 1
                    except CreditCap:
                        raise
                    except Exception as e:
                        log.warning("%s comments: %s", platform, e)
                views = [p["views"] or 0 for p in found]
                best = max(found, key=lambda p: p["views"] or 0) if found else None
                box[platform] = {"videos": len(found), "views": sum(views), "median_views": sorted(views)[len(views) // 2] if views else None,
                                 "comments": n_c, "top": {"title": best["title"][:140], "views": best["views"], "url": best["url"]} if best else None}
            boxes.append(box)
    except CreditCap as e:
        log.warning("%s: stopping TikTok/YouTube", e)
    _save_rows(lambda c: (c.execute("DELETE FROM social_posts WHERE fetched_date < ?", [today() - dt.timedelta(days=RETENTION_DAYS)]),
                           c.execute("DELETE FROM social_comments WHERE fetched_date < ?", [today() - dt.timedelta(days=RETENTION_DAYS)]),
                           upsert(c, "social_posts", list(posts.values())), upsert(c, "social_comments", list(comments.values()))))
    for p in posts.values():
        p.pop("_api_url", None)          # never stored
    return posts, comments, boxes


GENERIC = {"glp", "1", "tracker", "app", "ai", "calorie", "counter", "food", "weight", "loss", "fitness", "gym", "workout",
           "workouts", "planner", "personal", "trainer", "peptide", "log", "reminder", "diet", "meal", "meals", "protein",
           "macro", "nutrition", "health", "the", "my", "and", "shot", "dose", "injection", "medication", "tracking", "coach"}


def _brand(name: str) -> str:
    """The distinctive word of an app name, for a Facebook page search: 'Shotsy GLP-1 Tracker' -> 'Shotsy'."""
    toks = re.findall(r"[A-Za-z0-9]+", re.split(r"[:–|,]| - ", name)[0])
    for i, w in enumerate(toks):
        if w.lower() not in GENERIC:
            # a very short brand ("Pep") is ambiguous on Facebook: keep the next word ("Pep AI")
            return " ".join(toks[i:i + 2]) if len(w) <= 3 and i + 1 < len(toks) else w
    return ""


def _norm(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in {"app", "inc", "llc", "ltd", "the", "co", "tracker"}}


def collect_ads(sc, competitors: list[dict], n_apps: int = 12) -> list[dict]:
    """For the top competitors: find their Facebook page (verified when an ad links to the app's App Store id) and
    read their active US ads: how many, since when, on which platforms, with which message."""
    from ..sources.scrapecreators import CreditCap
    d, out, rows = today(), [], []
    try:
        for comp in competitors[:n_apps]:
            app_id, name = str(comp["app_id"]), comp["name"]
            short = _brand(name)
            res = {"app_id": app_id, "name": name, "page": None, "verified": False, "active_ads": 0}
            if not short:                      # a purely generic name ("Peptide Tracker Log") has no findable brand
                out.append(res)
                continue
            try:
                cands = sc.fb_companies(short).get("searchResults") or []
            except CreditCap:
                raise
            except Exception as e:
                log.warning("fb companies %r: %s", short, e)
                cands = []
            want = _norm(short) | _norm(comp.get("developer") or "")
            scored = sorted(((len(_norm(c.get("name")) & want) / max(1, len(_norm(c.get("name")))), c) for c in cands
                             if not c.get("page_is_deleted")), key=lambda x: -x[0])
            if not scored or scored[0][0] < 0.5:
                out.append(res)
                continue
            page = scored[0][1]
            ads = None
            for attempt in range(2):          # the ad library sometimes answers 500: one retry, then skip this app
                try:
                    ads = sc.fb_company_ads(page["page_id"])
                    break
                except CreditCap:
                    raise
                except Exception as e:
                    log.warning("fb ads %s (try %d): %s", page.get("name"), attempt + 1, e)
            if ads is None:
                res.update({"page": page.get("name"), "page_id": page["page_id"], "error": "ad library did not answer"})
                out.append(res)
                continue
            items = ads.get("results") or []
            mine = [a for a in items if app_id in str((a.get("snapshot") or {}).get("link_url") or "")]
            starts = [_iso(a.get("start_date_string")) or _ts(a.get("start_date")) for a in items]
            starts = [s for s in starts if s]
            texts = list(dict.fromkeys(((a.get("snapshot") or {}).get("body") or {}).get("text") or "" for a in items))
            texts = [t for t in texts if t and "{{" not in t][:3]
            plats = sorted({p for a in items for p in (a.get("publisher_platform") or [])})
            res.update({"page": page.get("name"), "page_id": page["page_id"], "verified": bool(mine), "active_ads": len(items),
                        "more": bool(ads.get("cursor")), "oldest_active_days": (dt.datetime.now(dt.UTC).replace(tzinfo=None) - min(starts)).days if starts else None,
                        "newest_days": (dt.datetime.now(dt.UTC).replace(tzinfo=None) - max(starts)).days if starts else None,
                        "platforms": plats, "messages": [t[:220] for t in texts],
                        "titles": list(dict.fromkeys((a.get("snapshot") or {}).get("title") or "" for a in items if (a.get("snapshot") or {}).get("title") and "{{" not in (a.get("snapshot") or {}).get("title")))[:3]})
            for a in items:
                sn = a.get("snapshot") or {}
                st = _iso(a.get("start_date_string")) or _ts(a.get("start_date"))
                rows.append({"ad_id": str(a.get("ad_archive_id")), "app_id": app_id, "page_id": page["page_id"], "page_name": page.get("name"),
                             "title": (sn.get("title") or "")[:300], "body": ((sn.get("body") or {}).get("text") or "")[:2000],
                             "start_date": st.date() if st else None, "is_active": bool(a.get("is_active")),
                             "platforms": ",".join(a.get("publisher_platform") or []), "link_url": (sn.get("link_url") or "")[:500], "fetched_date": d})
            out.append(res)
    except CreditCap as e:
        log.warning("%s: stopping Meta ads", e)
    if rows:
        _save_rows(lambda c: upsert(c, "ad_library", rows))
    return out
