"""On-demand deep research: type a keyword or topic, get an expanded, scored, clustered report.

Steps (each reported as progress): expand with DeepSeek in many directions -> App Store autocomplete for every
seed (real searches) -> DeepSeek labels (keep 'need') -> Astro popularity -> iTunes competitor pages for the best
terms -> rescore the universe -> cluster the found terms into topics -> report (+ metadata suggestion when an app
description is given). Runs are stored as JSON files under data/research/ so the dashboard can poll them."""
from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
import uuid

from ..config import settings
from ..db import connect_read_live
from .collect import is_latin
from .discover import add_to_universe, astro_popularity, label_terms, looks_like_app_name, search_terms, write_retry

log = logging.getLogger("apptopics.research")
RUNS = settings.db_path.parent / "research"
DEPTH = {"deep": {"seeds": 80, "search": 150, "second_pass": 60, "per_aspect": 14},
         "quick": {"seeds": 30, "search": 40, "second_pass": 0, "per_aspect": 5}}


def _save(run: dict) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{run['id']}.json").write_text(json.dumps(run, default=str))


def load_run(rid: str) -> dict | None:
    p = RUNS / f"{rid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def list_runs() -> list[dict]:
    if not RUNS.exists():
        return []
    out = []
    for p in sorted(RUNS.glob("*.json"), key=lambda p: -p.stat().st_mtime)[:30]:
        r = json.loads(p.read_text())
        out.append({**{k: r.get(k) for k in ("id", "topic", "status", "step", "pct", "started_at", "finished_at", "depth")},
                    "n_aspects": len(r.get("aspects") or []), "has_market": bool((r.get("result") or {}).get("market")),
                    "has_reddit": bool(((r.get("result") or {}).get("market") or {}).get("reddit"))})
    return out


def new_run(topic: str, app: str | None = None, depth: str = "deep", aspects: list[dict] | None = None,
            reddit: bool = True) -> dict:
    run = {"id": uuid.uuid4().hex[:10], "topic": topic.strip().lower(), "app": app, "depth": depth, "status": "running", "reddit": reddit,
           "step": "queued", "pct": 0, "log": [], "aspects": aspects or [], "started_at": dt.datetime.now().isoformat(timespec="seconds")}
    _save(run)
    return run


def run_research(run: dict, store: str = settings.store) -> dict:
    from ..sources.deepseek import DeepSeekClient
    from ..sources.itunes import ITunesClient
    cfg = DEPTH.get(run.get("depth", "deep"), DEPTH["deep"])
    topic = run["topic"]

    def step(name: str, pct: int, note: str | None = None):
        run["step"], run["pct"] = name, pct
        if note:
            run["log"].append(f"{dt.datetime.now():%H:%M:%S} {note}")
        _save(run)
    ds = None
    try:
        from .. import budget
        if not write_retry(lambda c: budget.guard(c, "deepseek", "research", 1)):
            raise RuntimeError("DeepSeek monthly cap reached: raise it in Budget & data")
        ds = DeepSeekClient()
        # 1. expand: per chosen aspect (each aspect's seeds + DeepSeek ideas inside it), else around the whole topic
        aspects = run.get("aspects") or []
        asp: dict[str, str] = {}          # term -> aspect it came from
        step("expanding with DeepSeek", 3)
        if aspects:
            ideas = []
            for a in aspects:
                for sd in a.get("seeds", []):
                    asp.setdefault(sd.lower(), a["name"])
                got = ds.expand_topic(f"{topic}: {a['name']} ({a.get('description', '')})", cfg["per_aspect"]) if ds.available else []
                for i in got:
                    asp.setdefault(i["term"], a["name"])
                ideas += got + [{"term": sd.lower(), "direction": a["name"]} for sd in a.get("seeds", [])]
        else:
            ideas = ds.expand_topic(topic, cfg["seeds"]) if ds.available else []
        seeds = list(dict.fromkeys([topic] + [i["term"] for i in ideas if is_latin(i["term"])]))
        direction = {i["term"]: i["direction"] for i in ideas}
        step("autocomplete", 8, f"{len(seeds)} seed phrases from DeepSeek")
        # 2. autocomplete (real searches)
        it, cand = ITunesClient(), {s: "seed" for s in seeds}
        for a in aspects:      # grounded aspects carry the real searches they rest on: no need to rediscover them
            for t in a.get("terms") or []:
                cand.setdefault(t, "evidence")
                asp.setdefault(t, a["name"])
        for n, s in enumerate(seeds, 1):
            try:
                for h in it.hints(s):
                    if is_latin(h) and not looks_like_app_name(h):
                        cand.setdefault(h, "autocomplete")
                        if s in asp:
                            asp.setdefault(h, asp[s])
            except Exception as e:
                log.warning("hints %r: %s", s, e)
            if n % 5 == 0:
                step("autocomplete", 8 + int((14 if cfg.get("second_pass") else 22) * n / len(seeds)))
        # deep: second autocomplete pass over the best first-pass suggestions (reaches further into the long tail)
        second = [c for c, src in cand.items() if src == "autocomplete"][: cfg.get("second_pass", 0)]
        for n, s2 in enumerate(second, 1):
            try:
                for h in it.hints(s2):
                    if is_latin(h) and not looks_like_app_name(h):
                        cand.setdefault(h, "autocomplete2")
                        if s2 in asp:
                            asp.setdefault(h, asp[s2])
            except Exception as e:
                log.warning("hints %r: %s", s2, e)
            if n % 5 == 0:
                step("autocomplete (2nd pass)", 22 + int(8 * n / max(1, len(second))))
        step("labelling", 30, f"{len(cand)} candidate searches")
        # 3. labels
        labels = label_terms(list(cand), store)
        need = [t for t in cand if labels.get(t, {}).get("label") == "need"]
        step("popularity (Astro)", 38, f"{len(need)} are real needs (brands/app names dropped)")
        # 4. popularity: Apple Ads list first, Astro for the rest
        con = connect_read_live()
        try:
            pop = {k: p for k, p in con.execute("""SELECT keyword, popularity FROM keywords WHERE popularity IS NOT NULL
                    AND keyword IN (SELECT unnest(?)) QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [need]).fetchall()}
            pop.update({k: p for k, p in con.execute("""SELECT keyword, popularity FROM keyword_metrics WHERE popularity IS NOT NULL
                    AND keyword IN (SELECT unnest(?)) QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [need]).fetchall() if k not in pop})
            searched = {k for (k,) in con.execute("SELECT DISTINCT keyword FROM search_results WHERE keyword IN (SELECT unnest(?))", [need]).fetchall()}
        finally:
            con.close()
        missing = [t for t in need if t not in pop]
        ast = astro_popularity(missing, store)
        pop.update({k: v["popularity"] for k, v in ast.items() if v.get("popularity") is not None})
        if not ast and missing:
            run["log"].append(f"Astro not running: long-tail popularity unknown for {len(missing)} terms")
        add_to_universe(need, f"research:{topic}", topic, store)
        # 5. competitor pages for the best terms
        ranked = sorted(need, key=lambda t: -(pop.get(t) or 0))
        ok = (lambda t: (pop.get(t) or 0) >= 10) if ast or not missing else (lambda t: pop.get(t) is None or pop[t] >= 10)
        if aspects:   # round-robin over aspects so every aspect gets competitor pages, best-searched first
            queues = [[t for t in ranked if asp.get(t) == a["name"] and t not in searched and ok(t)] for a in aspects]
            to_search = []
            while len(to_search) < cfg["search"] and any(queues):
                for q in queues:
                    if q and len(to_search) < cfg["search"]:
                        to_search.append(q.pop(0))
        else:
            to_search = [t for t in ranked if t not in searched and ok(t)][: cfg["search"]]
        step("competitor pages (iTunes)", 45, f"{len(pop)} with popularity; searching {len(to_search)} new terms")
        search_terms(to_search, store, progress=lambda i, n: step("competitor pages (iTunes)", 45 + int(40 * i / max(1, n))) if i % 5 == 0 else None)
        # 6. score
        step("scoring", 86)
        from .score import score_all
        write_retry(lambda c: score_all(c, store))
        # 7. report
        step("clustering + report", 88)
        run["result"] = build_report(topic, need, pop, direction, run.get("app"), store)
        # 8. market report: crowdedness per aspect, competitors, prices, feature map, happy users, highlights
        from .market import build_market
        names = [a["name"] for a in aspects]
        term_aspect = {t: asp.get(t, names[0] if names else topic) for t in need}
        phases = {"competitor prices (App Store pages)": (89, 3), "feature map (DeepSeek)": (92, 3),
                  "what happy users say (4-5 star reviews)": (95, 3)}
        run["result"]["market"] = build_market(topic, aspects, term_aspect, run.get("app"), store,
                                               step=lambda n, p: step(n, phases.get(n, (90, 0))[0] + int(phases.get(n, (90, 0))[1] * p / 100)))
        if run.get("reddit"):
            _reddit_step(run, step)
        _verdict_step(run, step)
        run["status"], run["pct"], run["step"] = "done", 100, "done"
    except Exception as e:
        run["status"], run["step"] = "error", f"error: {e}"
        run["log"].append(traceback.format_exc()[-1500:])
        log.exception("research failed")
    _record_deepseek(ds, "research_expand", topic)
    run["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _save(run)
    return run


def _record_deepseek(ds, job: str, note: str) -> None:
    """Book a DeepSeek client's usage against the monthly cap (month-to-date spend must see every call)."""
    if ds is None or not getattr(ds, "available", False) or not (ds.usage_in + ds.usage_out):
        return
    from .. import budget
    try:
        write_retry(lambda c: budget.record(c, "deepseek", job, ds.usage_in + ds.usage_out, ds.cost_usd(), note), tries=3, wait=1)
    except Exception as e:
        log.warning("spend record failed: %s", e)


def score_items(terms: list[str], pop: dict, direction: dict, store: str) -> list[dict]:
    """One row per scored search: the latest factor values and index verdicts, plus the top-3 apps on its page."""
    from .index import searches_per_day
    con = connect_read_live()
    try:
        d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='composite' AND entity_type='keyword'").fetchone()[0]
        rows = con.execute("""
            WITH fresh AS (SELECT DISTINCT metric FROM factor_values WHERE entity_type='keyword' AND snapshot_date=?),
                 r AS (SELECT entity_id, metric, value, meta FROM factor_values
                       WHERE entity_type='keyword' AND entity_id IN (SELECT unnest(?)) AND (snapshot_date >= ? OR metric NOT IN (SELECT metric FROM fresh))
                       QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1)
            SELECT entity_id, map_from_entries(list((metric, value))), map_from_entries(list((metric, meta))) FROM r GROUP BY entity_id""",
            [d, terms, d]).fetchall()
        top_apps = {}
        for kw, name, rc in con.execute("""SELECT keyword, name, rating_count FROM search_results WHERE keyword IN (SELECT unnest(?)) AND rank<=3
                QUALIFY dense_rank() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1 ORDER BY keyword, rank""", [terms]).fetchall():
            top_apps.setdefault(kw, []).append(f"{name} ({rc or 0:,})")
    finally:
        con.close()
    items = []
    for kw, vals, metas in rows:
        v = dict(vals)
        m = {k: (json.loads(x) if x else {}) for k, x in dict(metas).items()}
        p = v.get("demand") or pop.get(kw)
        items.append({"keyword": kw, "popularity": p, "searches_day": round(searches_per_day(p)) if p else None,
                      "trend": v.get("trending"), "difficulty": v.get("difficulty"), "weak_spots": v.get("weak_spots"),
                      "band_usd": (m.get("attainable") or {}).get("attainable_usd"), "p5k": v.get("breadth"),
                      "opp": v.get("opp_score"), "opp_grade": (m.get("opp_score") or {}).get("grade"),
                      "opp_action": (m.get("opp_score") or {}).get("action"), "opp_reason": (m.get("opp_score") or {}).get("reason"),
                      "parts": (m.get("opp_score") or {}).get("parts"),
                      "aso": v.get("aso_score"), "aso_action": (m.get("aso_score") or {}).get("action"),
                      "aso_reason": (m.get("aso_score") or {}).get("reason"), "direction": direction.get(kw), "top_apps": top_apps.get(kw, [])})
    return items


def build_report(topic: str, terms: list[str], pop: dict, direction: dict, app: str | None, store: str) -> dict:
    from ..sources.deepseek import DeepSeekClient
    items = score_items(terms, pop, direction, store)
    unscored = [t for t in terms if t not in {i["keyword"] for i in items}]
    by_opp = sorted([i for i in items if i["opp"]], key=lambda i: -i["opp"])
    by_aso = sorted([i for i in items if i["aso"] and i["aso_action"] != "Skip"], key=lambda i: -i["aso"])
    rising = sorted([i for i in items if (i["trend"] or 0) > 1], key=lambda i: -(i["trend"] or 0))[:15]
    # cluster scored terms into topics
    clusters = []
    ds = DeepSeekClient()
    pool = [i["keyword"] for i in sorted(items, key=lambda i: -(i["popularity"] or 0))][:250]
    if ds.available and pool:
        try:
            for c in ds.cluster_topics(pool):
                mem = [i for i in items if i["keyword"] in set(c.get("keywords", []))]
                if not mem:
                    continue
                best = max(mem, key=lambda i: (i["opp"] or 0))
                clusters.append({"label": c.get("label"), "description": c.get("description"), "n": len(mem),
                                 "best_keyword": best["keyword"], "best_opp": best["opp"], "best_grade": best["opp_grade"],
                                 "total_searches_day": sum(i["searches_day"] or 0 for i in mem),
                                 "max_weak_spots": max((i["weak_spots"] or 0) for i in mem),
                                 "keywords": [i["keyword"] for i in sorted(mem, key=lambda i: -(i["popularity"] or 0))][:12]})
            clusters.sort(key=lambda c: -(c["best_opp"] or 0))
        except Exception as e:
            log.warning("clustering failed: %s", e)
    meta_suggestion = None
    if app and ds.available and by_aso:
        try:
            meta_suggestion = ds.suggest_metadata(app, [{"keyword": i["keyword"], "searches_day": i["searches_day"], "weak_spots": i["weak_spots"]}
                                                        for i in by_aso[:25]])
        except Exception as e:
            log.warning("metadata suggestion failed: %s", e)
    _record_deepseek(ds, "research_report", topic)
    return {"topic": topic, "found": len(terms), "scored": len(items), "unscored": len(unscored),
            "build_now": [i for i in by_opp if i["opp_action"] == "Build now"][:20], "top_opportunities": by_opp[:40],
            "unproven": sorted([i for i in items if i["opp_action"] == "Unproven"], key=lambda i: -(i["popularity"] or 0))[:40],
            "top_aso": by_aso[:40], "rising": rising, "clusters": clusters[:25], "metadata_suggestion": meta_suggestion}


REDDIT_CREDITS_PER_ASPECT, REDDIT_CREDITS_MAX = 16, 80


def _credits_left() -> int | None:
    """Last ScrapeCreators balance seen (stored with each report's Reddit section), so a run splits what is really left."""
    best = None
    for r in list_runs():
        rd = (((load_run(r["id"]) or {}).get("result") or {}).get("market") or {}).get("reddit") or {}
        if rd.get("credits_remaining") is not None and (best is None or (rd.get("fetched") or "") >= best[0]):
            best = (rd.get("fetched") or "", int(rd["credits_remaining"]))
    return best[1] if best else None


def _reddit_step(run: dict, step) -> None:
    """9. Reddit (ScrapeCreators): posts + comments per aspect, merged with the App Store reviews into 'Voice of users'.
    Never fails the run: a missing key, the monthly cap or no credits left only adds a note."""
    from .. import budget
    from ..sources.scrapecreators import ScrapeCreators
    from .reddit_voice import add_reddit
    if not ScrapeCreators().available:
        run["log"].append("Reddit skipped: SCRAPECREATORS_API_KEY missing")
        return
    credits = min(REDDIT_CREDITS_MAX, REDDIT_CREDITS_PER_ASPECT * max(1, len(run.get("aspects") or [])))
    left = _credits_left()
    if left is not None:
        credits = min(credits, left)
    if credits < 6:
        run["log"].append(f"Reddit skipped: only {credits} ScrapeCreators credits left")
        return
    from ..db import connect_read_live
    with connect_read_live() as con:
        ok, why = budget.check(con, "scrapecreators", budget.estimate_job("scrapecreators", "reddit_research", credits))
    if not ok:
        run["log"].append(f"Reddit skipped: {why}")
        return
    step("Reddit (ScrapeCreators)", 99, f"Reddit: up to {credits} credits")
    _save(run)                                  # add_reddit works on the saved run
    try:
        got = add_reddit(run["id"], max_credits=credits)
        run["result"] = load_run(run["id"])["result"]
        run["log"].append(f"Reddit: {got['posts']} posts, {got['comments']} comments, {got['credits_used']} credits (${got['usd']:.3f})")
    except Exception as e:
        log.warning("reddit step: %s", e)
        run["log"].append(f"Reddit skipped: {e}")


def _verdict_step(run: dict, step) -> None:
    """10. Verdict: social evidence into the model, rescore, one grade per aspect and the overall call. Never fails the run."""
    from .verdict import finalize
    step("verdict", 99, "verdict: aspect grades + overall call")
    _save(run)                                  # finalize works on the saved run
    try:
        v = finalize(run["id"])
        saved = load_run(run["id"])
        run["result"], run["log"] = saved["result"], saved.get("log") or run["log"]
        run["log"].append(f"{dt.datetime.now():%H:%M:%S} verdict {v.get('grade')}: {v.get('call') or ''}")
    except Exception as e:
        log.warning("verdict step: %s", e)
        run["log"].append(f"Verdict skipped: {e}")


def _guess_aspect(term: str, aspects: list[dict], topic: str) -> str:
    """For runs saved before term_aspect was stored: the aspect sharing the most words with the term."""
    if not aspects:
        return topic
    words = set(term.split())
    def overlap(a):
        bag = set(a["name"].lower().split()) | {w for sd in a.get("seeds", []) for w in sd.split()}
        return len(words & bag)
    return max(aspects, key=overlap)["name"]


def rebuild_market(run_id: str, store: str = settings.store) -> dict:
    """Recompute only the market report of a finished run (after a fix, or to refresh prices and reviews)."""
    from .market import build_market
    run = load_run(run_id)
    if not run or not run.get("result"):
        raise ValueError(f"run {run_id} has no result")
    res, aspects = run["result"], run.get("aspects") or []
    ta = (res.get("market") or {}).get("term_aspect")
    if not ta:
        terms = list(dict.fromkeys([i["keyword"] for i in res.get("top_opportunities", []) + res.get("top_aso", [])]
                                   + [k for c in res.get("clusters", []) for k in c.get("keywords", [])]))
        ta = {t: _guess_aspect(t, aspects, run["topic"]) for t in terms}
    run["step"] = "rebuilding market report"
    _save(run)
    res["market"] = build_market(run["topic"], aspects, ta, run.get("app"), store, step=lambda n, p: log.info("%s %d%%", n, p))
    run["step"] = "done"
    _save(run)
    return res["market"]
