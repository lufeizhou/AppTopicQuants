"""Verdict for a research run: one grade per aspect (built from five visible parts) and one overall call, written
conclusions-first so the report can show the answer before the evidence.

Aspect verdict (0-100), each part 0-1, weights shown in the report:
  opening        30%  crowdedness band: Emerging 1.0 · Empty 0.7 (nobody serves it: unproven, not bad) · Crowded 0.35 · Saturated 0.1
  people care    25%  TikTok + YouTube views about the aspect on a log scale (10k views -> 0, 10M -> 1)
  newcomers win  20%  apps under 2 years old earning >= $5k among the aspect's competitors (4 -> 1)
  someone pays   15%  competitors of the aspect with verified, active Meta ads (3 -> 1)
  white space    10%  unmet wishes users voice for the aspect that no competitor offers (3 -> 1)
Grades: A >= 80, B >= 65, C >= 50, D >= 35, else E. An Empty aspect is also flagged unproven.

The keyword scores in the same report answer a narrower question (would a search-led app for this phrase pay?), so
the two can disagree: the aspect verdict explains why.

finalize(run_id) runs after the social step: it stores the run's social evidence per keyword (keyword_social), recomputes
the social factors and the index, refreshes the report's keyword tables, then writes aspect verdicts and the overall
verdict (DeepSeek words the call; the grade is ours).
"""
from __future__ import annotations

import datetime as dt
import logging
import math

from ..config import settings
from ..db import today, upsert
from .discover import write_retry
from .research import _save, load_run

log = logging.getLogger(__name__)

_V = settings.section("verdict")   # config/model.toml [step8.verdict]
WEIGHTS = {k: float(_V.get(k, d)) for k, d in (("opening", 0.30), ("people_care", 0.25), ("newcomers_win", 0.20),
                                               ("someone_pays", 0.15), ("white_space", 0.10))}
LABELS = {"opening": "Opening", "people_care": "People care", "newcomers_win": "Newcomers win", "someone_pays": "Someone pays ads", "white_space": "White space"}
OPENING = {"Emerging": float(_V.get("band_emerging", 1.0)), "Empty": float(_V.get("band_empty", 0.7)),
           "Crowded": float(_V.get("band_crowded", 0.35)), "Saturated": float(_V.get("band_saturated", 0.1))}
TARGET = float(settings.weights().get("target", {}).get("monthly_usd", 5000))


ORDER = {"A": 0, "B": 1, "?": 2, "C": 3}


def _aspect_grade(row: dict, n_young: int, unproven: bool) -> tuple[str, dict, str]:
    """Same evidence checks as a search (index.earn_grade, decided 2026-11-05): money = median p5-p20 band of the
    aspect's searches, newcomers = young apps earning $5k+, traffic = the aspect's searches/day summed, build = its
    best judged search. The 0-100 aspect score only orders aspects inside a grade."""
    from .index import earn_grade, popularity_for
    return earn_grade(row.get("small_apps_earn"), n_young, popularity_for(row.get("searches_day")), row.get("build"),
                      unknown="no app serves it yet (unproven)" if unproven else None)


def _views(m: dict) -> dict[str, dict]:
    out = {}
    for s in ((m.get("social") or {}).get("aspects") or []):
        tt, yt = s.get("tiktok") or {}, s.get("youtube") or {}
        tv, yv = tt.get("views"), yt.get("views")
        if tv is None and yv is None:            # both searches failed: unknown, not zero
            continue
        out[s["aspect"]] = {"views": (tv or 0) + (yv or 0), "videos": (tt.get("videos") or 0) + (yt.get("videos") or 0),
                            "median": max(tt.get("median_views") or 0, yt.get("median_views") or 0)}
    return out


def aspect_verdicts(run: dict) -> list[dict]:
    m = run["result"]["market"]
    comps = m.get("competitors") or []
    ads = {a["app_id"]: a for a in (m.get("ads") or []) if a.get("active_ads") and a.get("verified")}
    views = _views(m)
    wishes = (m.get("voice") or {}).get("wishes") or []
    out = []
    for row in m.get("aspects") or []:
        name = row["aspect"]
        mine = [c for c in comps if name in (c.get("aspects") or [])]
        young = [c for c in mine if c.get("age_months") is not None and c["age_months"] <= _V.get("young_months", 24) and (c.get("est") or 0) >= TARGET]
        paying = [c for c in mine if c["app_id"] in ads]
        unmet = [w for w in wishes if w.get("unmet") and (w.get("aspect") or "") == name]
        v = views.get(name, {})
        parts = {"opening": OPENING.get(row.get("band"), 0.5),
                 "people_care": max(0.0, min(1.0, (math.log10(max(v.get("views", 0), 1)) - _V.get("views_zero_log", 4))
                                             / (_V.get("views_full_log", 7) - _V.get("views_zero_log", 4)))) if v else None,
                 "newcomers_win": min(1.0, len(young) / _V.get("young_full", 4)),
                 "someone_pays": min(1.0, len(paying) / _V.get("advertisers_full", 3)) if m.get("ads") is not None else None,
                 "white_space": min(1.0, len(unmet) / _V.get("wishes_full", 3))}
        known = {k: p for k, p in parts.items() if p is not None}
        wsum = sum(WEIGHTS[k] for k in known) or 1.0
        score = round(100 * sum(WEIGHTS[k] * known[k] for k in known) / wsum)
        unproven = row.get("band") == "Empty"
        why = []
        why.append({"Emerging": "some proven apps, room left", "Empty": "no app serves it yet (unproven)", "Crowded": f"{row.get('established')} established apps",
                    "Saturated": "giants with no weak spots"}.get(row.get("band"), ""))
        if v:
            why.append(f"{v['views'] / 1e6:.1f}M video views" if v["views"] >= 1e6 else f"{v['views'] / 1e3:.0f}k video views")
        why.append(f"{len(young)} young app{'s' if len(young) != 1 else ''} earn ${TARGET / 1000:.0f}k+" if young else f"no young app earns ${TARGET / 1000:.0f}k+ yet")
        if paying:
            why.append(f"{len(paying)} competitor{'s' if len(paying) > 1 else ''} pay for ads")
        if unmet:
            why.append(f"{len(unmet)} unmet wish{'es' if len(unmet) > 1 else ''}")
        grade, checks, grade_why = _aspect_grade(row, len(young), unproven)
        out.append({"aspect": name, "score": score, "grade": grade, "checks": checks, "grade_why": grade_why, "unproven": unproven, "parts": parts, "weights": WEIGHTS,
                    "evidence": {"band": row.get("band"), "views": v.get("views"), "videos": v.get("videos"), "searches_day": row.get("searches_day"),
                                 "young_winners": [{"name": c["name"], "est": c.get("est"), "age_months": c.get("age_months")} for c in sorted(young, key=lambda c: -(c.get("est") or 0))[:5]],
                                 "advertisers": [{"name": c["name"], "days": ads[c["app_id"]].get("oldest_active_days")} for c in paying],
                                 "unmet": [w.get("wish") for w in unmet[:4]], "small_apps_earn": row.get("small_apps_earn"),
                                 "best_search": row.get("best_search"), "best_grade": row.get("best_grade")},
                    "why": " · ".join(w for w in why if w)})
    out.sort(key=lambda a: (ORDER.get(a["grade"], 3), -a["score"]))
    return out


def overall_verdict(run: dict, aspects: list[dict], ds) -> dict:
    """DeepSeek words the call from the facts; the grade is the best aspect's grade (A can earn / B maybe / C no)."""
    m, topic = run["result"]["market"], run["topic"]
    best = aspects[0] if aspects else None          # sorted best grade first, then score
    grade = best["grade"] if best else "C"
    res = run["result"]
    kw = [{k: i.get(k) for k in ("keyword", "opp_grade", "opp_action", "searches_day", "band_usd", "p5k")} for i in (res.get("top_opportunities") or [])[:8]]
    ctx = {"topic": topic, "our_app": run.get("app") or "", "overall_grade": grade,
           "aspect_verdicts": [{k: a[k] for k in ("aspect", "score", "grade", "unproven", "why")} | {"evidence": a["evidence"]} for a in aspects],
           "positioning_options": [{k: o.get(k) for k in ("name", "one_liner", "aspects", "price")} for o in (m.get("highlights") or [])],
           "young_winners": [{"name": c["name"], "est": c.get("est"), "age_months": c.get("age_months")} for c in (m.get("competitors") or [])
                             if c.get("age_months") is not None and c["age_months"] <= 24 and (c.get("est") or 0) >= TARGET][:8],
           "advertisers": [{"name": a["name"], "active_ads": a.get("active_ads"), "oldest_active_days": a.get("oldest_active_days"), "verified": a.get("verified")}
                           for a in (m.get("ads") or []) if a.get("active_ads")],
           "search_led_keyword_scores": kw, "prices": (m.get("prices") or {}).get("suggested"),
           "top_pains": [{k: p.get(k) for k in ("pain", "aspect", "strength")} for p in ((m.get("voice") or {}).get("pains") or [])[:6]],
           "unmet_wishes": [w.get("wish") for w in ((m.get("voice") or {}).get("wishes") or []) if w.get("unmet")][:6],
           "metadata_suggestion": res.get("metadata_suggestion")}
    words = {}
    for attempt in range(2):
        try:
            words = ds.verdict(ctx)
            if words.get("call"):
                break
        except Exception as e:
            log.warning("verdict attempt %d failed: %s", attempt + 1, e)
    return {"grade": grade, "best_aspect": best["aspect"] if best else None, "computed_at": dt.datetime.now().isoformat(timespec="seconds"),
            "how": "grade = best aspect's grade: A when its apps earn $5k+, 2+ young apps earn $5k+, ~17+ searches/day and it is buildable; B when money exists but a check fails; ? when nobody serves it yet (not sure); C otherwise. The words are DeepSeek's reading of the facts below", **words}


def write_keyword_social(run: dict) -> int:
    """Store the run's per-aspect video views and the market's ad activity per keyword, so the scoring model can use them."""
    m = run["result"]["market"]
    views = _views(m)
    ads = m.get("ads")
    checked = len(ads) if ads is not None else None
    verified = [a for a in (ads or []) if a.get("active_ads") and a.get("verified")]
    oldest = max((a.get("oldest_active_days") or 0 for a in verified), default=None) if verified else None
    rows, d, src = [], today(), f"research:{run['id']}"
    for kw, asp in (m.get("term_aspect") or {}).items():
        v = views.get(asp)
        if v is None and checked is None:
            continue
        rows.append({"keyword": kw, "source": src, "fetched_date": d, "views": v["views"] if v else None, "median_views": v["median"] if v else None,
                     "videos": v["videos"] if v else None, "ads_checked": checked, "ads_verified": len(verified) if checked is not None else None,
                     "ads_oldest_days": oldest})
    if rows:
        write_retry(lambda c: upsert(c, "keyword_social", rows))
    return len(rows)


def refresh_scores(run: dict, store: str = settings.store) -> None:
    """Re-read the keyword scores of a report after a rescoring (no DeepSeek, no clustering)."""
    from .research import score_items
    res = run["result"]
    old = {i["keyword"]: i for i in res.get("top_opportunities", []) + res.get("top_aso", [])}
    terms = list(dict.fromkeys(list(old) + [k for c in res.get("clusters", []) for k in c.get("keywords", [])]
                               + list((res.get("market") or {}).get("term_aspect") or {})))
    pop = {k: i.get("popularity") for k, i in old.items()}
    direction = {k: i.get("direction") for k, i in old.items()}
    items = score_items(terms, pop, direction, store)
    by = {i["keyword"]: i for i in items}
    by_opp = sorted([i for i in items if i["opp"]], key=lambda i: ({"A": 0, "B": 1, "?": 2}.get(i.get("opp_grade"), 3), -i["opp"]))   # grade first, score inside
    res["build_now"] = [i for i in by_opp if i["opp_action"] == "Build now"][:20]
    res["top_opportunities"] = by_opp[:40]
    res["top_aso"] = sorted([i for i in items if i["aso"] and i["aso_action"] != "Skip"], key=lambda i: -i["aso"])[:40]
    res["unproven"] = sorted([i for i in items if (i.get("opp_reason") or "").startswith("not sure: too few")], key=lambda i: -(i["popularity"] or 0))[:40]
    for c in res.get("clusters", []):
        mem = [by[k] for k in c.get("keywords", []) if k in by]
        if mem:
            best = max(mem, key=lambda i: (i["opp"] or 0))
            c.update({"best_keyword": best["keyword"], "best_opp": best["opp"], "best_grade": best["opp_grade"]})
    ta = (res.get("market") or {}).get("term_aspect") or {}
    for row in (res.get("market") or {}).get("aspects") or []:
        b = row.get("best_search")
        if b in by:
            row["best_grade"], row["best_score"] = by[b]["opp_grade"], round(by[b]["opp"] or 0)
        mine = [by[k] for k, a in ta.items() if a == row["aspect"] and k in by]   # aspect grade inputs (build, A searches)
        row["build"] = max((i["buildability"] for i in mine if i.get("buildability") is not None), default=None)
        row["n_a"] = sum(1 for i in mine if i.get("opp_grade") == "A")
    res["rescored_at"] = dt.datetime.now().isoformat(timespec="seconds")


def finalize(run_id: str, store: str = settings.store, rescore: bool = True) -> dict:
    """Social evidence -> factors -> index -> report tables -> aspect verdicts -> overall verdict. Safe to rerun."""
    from .. import budget
    from ..sources.deepseek import DeepSeekClient
    from .index import build_index
    from .score import run_metrics
    from .topics import build_topic_index
    run = load_run(run_id)
    if not run or not (run.get("result") or {}).get("market"):
        raise ValueError(f"run {run_id} has no market report")
    n = write_keyword_social(run)
    if rescore:
        def _rescore(c):
            run_metrics(c, store, only=["social_views", "ad_activity"])
            build_index(c, store)
            build_topic_index(c, store)
        write_retry(_rescore)
        refresh_scores(run, store)
    m = run["result"]["market"]
    ds = DeepSeekClient()
    if not m.get("highlights") and ds.available:      # positioning options are written once, with all evidence in hand
        from .reddit_voice import _highlights
        _highlights(run, ds)
    m["aspect_verdicts"] = aspect_verdicts(run)
    m["verdict"] = overall_verdict(run, m["aspect_verdicts"], ds) if ds.available else {"grade": m["aspect_verdicts"][0]["grade"] if m["aspect_verdicts"] else "C"}
    if ds.available:
        write_retry(lambda c: budget.record(c, "deepseek", "verdict", ds.usage_in + ds.usage_out, ds.cost_usd(), run["topic"]), tries=3, wait=1)
    run["log"] = (run.get("log") or []) + [f"{dt.datetime.now():%H:%M:%S} verdict: {m['verdict'].get('grade')} · {n} keywords with social evidence"]
    if run.get("status") == "done":
        run["step"], run["pct"] = "done", 100     # the dashboard marks the run busy while this step runs
    _save(run)
    return m["verdict"]
