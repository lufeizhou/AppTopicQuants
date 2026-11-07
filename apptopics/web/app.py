"""Local dashboard: FastAPI JSON API + one static HTML page."""
from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .. import budget
from ..config import ROOT, settings
from ..db import connect, connect_read, read_copy_path
from ..metrics import metrics

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="AppTopicsQuants")
app.add_middleware(GZipMiddleware, minimum_size=2048)


def _spawn(args: list[str], log_path: Path) -> None:
    """Run a CLI job detached from the server; its output goes to logs/."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as out:
        subprocess.Popen([sys.executable, "-m", "apptopics.cli", *args], stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT), start_new_session=True)


def _con():
    con, fallback = connect_read()
    app.state.fallback = fallback
    return con


@app.get("/")
def index():
    # no-cache: browsers otherwise keep an old copy of the page (and its JS) for days after an update
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/research.js")
def research_js():
    return FileResponse(STATIC / "research.js", media_type="application/javascript", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/overview")
def overview(store: str = settings.store):
    con = _con()
    try:
        def q(sql, *a):
            return con.execute(sql, list(a)).fetchone()
        last_kw = q("SELECT max(snapshot_date), count(DISTINCT snapshot_date) FROM keywords WHERE store=?", store)
        n_kw = q("SELECT count(*) FROM keywords WHERE store=? AND snapshot_date=?", store, last_kw[0])[0] if last_kw[0] else 0
        n_search = q("SELECT count(DISTINCT keyword) FROM search_results WHERE store=?", store)[0]
        n_apps = q("SELECT count(DISTINCT app_id) FROM apps WHERE store=?", store)[0]
        n_play = q("SELECT count(DISTINCT keyword) FROM play_search WHERE country=?", store)[0]
        n_scored = q("SELECT count(*) FROM factor_values WHERE metric='composite' AND snapshot_date=(SELECT max(snapshot_date) FROM factor_values WHERE metric='composite' AND entity_type='keyword')")[0]
        jobs = con.execute("SELECT job, store, started_at, finished_at, rows, ok, error FROM collection_log ORDER BY started_at DESC LIMIT 20").fetchall()
        return {"store": store, "read_fallback": getattr(app.state, "fallback", False), "keywords": n_kw, "keyword_snapshots": last_kw[1], "last_keyword_snapshot": str(last_kw[0]),
                "keywords_with_search": n_search, "apps": n_apps, "play_keywords": n_play, "scored": n_scored,
                "metrics": [{"name": n, "description": c.description, "higher_is_better": c.higher_is_better} for n, c in metrics.items()],
                "weights": settings.weights(),
                "jobs": [dict(zip(["job", "store", "started_at", "finished_at", "rows", "ok", "error"], map(lambda v: str(v) if isinstance(v, dt.datetime) else v, j))) for j in jobs]}
    finally:
        con.close()


_CACHE: dict = {}
SLIM = {"composite": ("excluded", "excluded_reason", "label"),
        "opp_score": ("grade", "action", "reason", "parts", "n_members", "n_strong", "searches_per_day", "best", "traffic_adj", "n_hidden", "hidden_kinds", "hidden"),
        "aso_score": ("grade", "action", "reason"), "attainable": ("attainable_usd",),
        **{k: ("why", "early", "blocks", "value_cat", "category", "coverage", "n") for k in
           ("idx_popularity", "idx_paying", "idx_momentum", "idx_demand")}}
IDX = ("idx_popularity", "idx_paying", "idx_momentum", "idx_demand")


def no_dollars(text: str | None, row: dict) -> str | None:
    """Step 3 (2026-11-06): the dashboard shows Popularity / Paying / Momentum scores, not dollar guesses. The grade
    still uses the dollar model until Step 6, so its reasons are reworded here: money facts become the Paying score."""
    import re
    if not text:
        return text
    pay = row.get("idx_paying")
    pay_s = f"paying {pay:.0f}" if pay is not None else "paying not measured yet"
    text = re.sub(r"p5-p20 apps earn ~\$[\d,]+/mo", pay_s, text)
    text = re.sub(r"(\d+) young apps? earns? \$5k\+", lambda m: f"{m.group(1)} young app{'s' if m.group(1) != '1' else ''} earning well", text)
    text = re.sub(r"~?(\d+) young apps already earn \$5k\+", r"\1 young apps earning well", text)
    text = text.replace("no young app earns $5k+ yet", "no young app earning well yet")
    text = re.sub(r"band ?< ?\$[\d,]+", "low earnings", text)
    text = re.sub(r"p5[-–]p20 band under \$1k/month", "low earnings", text)
    text = re.sub(r"popularity (\d+) \(~([\d,]+)/day\)", r"~\2 searches/day (search popularity \1)", text)   # not the app Popularity score
    return text


def _scrub(r: dict) -> dict:
    m = r.get("meta") or {}
    for k in ("opp_score", "aso_score"):
        if isinstance(m.get(k), dict) and m[k].get("reason"):
            m[k]["reason"] = no_dollars(m[k]["reason"], r)
    if isinstance(m.get("composite"), dict) and m["composite"].get("excluded_reason"):
        m["composite"]["excluded_reason"] = no_dollars(m["composite"]["excluded_reason"], r)
    return r


def _db_version(con) -> float:
    """Changes whenever the database file (or its WAL / the read copy) changes: cheap cache key."""
    import os
    paths = [Path(settings.db_path), Path(str(settings.db_path) + ".wal"), read_copy_path()]
    return max((os.path.getmtime(x) for x in paths if x.exists()), default=0.0)


def _table(con, store: str, entity: str) -> list[dict]:
    """All entities with their latest factor values + slimmed meta, built once per database version."""
    key = (store, entity)
    ver = _db_version(con)
    hit = _CACHE.get(key)
    if hit and hit[0] == ver:
        return hit[1]
    rows = con.execute("""
        WITH dc AS (SELECT max(snapshot_date) d FROM factor_values WHERE metric='composite' AND entity_type='keyword'),
             fresh AS (SELECT DISTINCT metric FROM factor_values WHERE entity_type=? AND snapshot_date=(SELECT d FROM dc)),
             f AS (
          SELECT entity_id, metric, value, meta FROM factor_values WHERE entity_type=?
            AND (snapshot_date >= (SELECT d FROM dc) OR metric NOT IN (SELECT metric FROM fresh))   -- same freshness rule as the index
          QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1)
        SELECT entity_id, map_from_entries(list((metric, value))), map_from_entries(list((metric, meta))) FROM f GROUP BY entity_id""", [entity, entity]).fetchall()
    labels, members = {}, {}
    if entity == "topic":
        labels = dict(con.execute("SELECT topic_id, label FROM topics").fetchall())
        for t, k in con.execute("SELECT topic_id, keyword FROM topic_keywords WHERE store=?", [store]).fetchall():
            members.setdefault(t, []).append(k)
    out = []
    for kw, vals, metas in rows:
        vals = dict(vals)
        meta = {}
        for m, keep in SLIM.items():
            raw = dict(metas).get(m)
            if raw:
                d = json.loads(raw)
                meta[m] = {k: d.get(k) for k in keep if k in d}
                if m in IDX and isinstance(d.get("parts"), list):   # compact ingredients: [percentile, raw value]
                    meta[m]["p"] = [[x.get("pct"), x.get("raw")] for x in d["parts"]]
        out.append({"keyword": kw, "label": labels.get(kw, kw), "members": members.get(kw, []), "_hidden": bool((meta.get("composite") or {}).get("excluded")),
                    **vals, "meta": meta})
    if entity == "topic":   # topic scores = its best member's (the member that sets the topic's grade)
        kw = {r["keyword"]: r for r in _table(con, store, "keyword")}
        for r in out:
            b = kw.get(((r.get("meta") or {}).get("opp_score") or {}).get("best") or "")
            if b:
                for k in IDX:
                    if b.get(k) is not None:
                        r[k] = b[k]; r["meta"][k] = (b.get("meta") or {}).get(k)
    out = [_scrub(r) for r in out]
    out.sort(key=lambda r: (r.get("opp_score") or 0, r.get("composite", -99)), reverse=True)
    _CACHE[key] = (ver, out)
    return out


@app.get("/api/keywords")
def keywords(store: str = settings.store, limit: int = Query(300, le=20000), max_brandness: float | None = None,
             min_popularity: int = 5, q: str | None = None, include_excluded: bool = False, entity: str = "keyword"):
    con = _con()
    try:
        table = _table(con, store, entity)
    finally:
        con.close()
    ql = (q or "").lower().strip()
    out = []
    for r in table:
        if ql and ql not in r["keyword"] and ql not in r["label"].lower() and not any(ql in m for m in r["members"]):
            continue
        if (r.get("demand") or 0) < min_popularity:
            continue
        if max_brandness is not None and (r.get("brandness") or 0) > max_brandness:
            continue
        if not include_excluded and r["_hidden"]:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return JSONResponse(out)


@app.get("/api/explain")
def explain(store: str = settings.store):
    """What every number is made of: the index recipes (ingredients, weights, notes) and, for every numeric column,
    101 cut points over all searches so the page can say "better than X% of searches" and draw the 5 blocks."""
    from ..metrics.market_scores import SPECS
    con = _con()
    try:
        table = _table(con, store, "keyword")
    finally:
        con.close()
    hit = _CACHE.get(("explain", store))
    if hit and hit[0] is table:
        return JSONResponse(hit[1])
    cols: dict[str, list[float]] = {}
    for r in table:
        for k, v in r.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not k.startswith("_"):
                cols.setdefault(k, []).append(float(v))
    quant = {}
    for k, vs in cols.items():
        vs.sort()
        n = len(vs)
        if n >= 20:
            quant[k] = {"n": n, "q": [round(vs[min(n - 1, int(i / 100 * n))], 4) for i in range(101)]}
    out = {"specs": {s.name: s.spec() for s in SPECS.values()}, "quant": quant}
    _CACHE[("explain", store)] = (table, out)
    return JSONResponse(out)


def _full(con, entity_id: str, entity: str) -> dict:
    """Latest factor values and FULL meta for one entity (the list endpoint sends a slimmed meta)."""
    vals, meta = {}, {}
    for m, v, mt in con.execute("""SELECT metric, value, meta FROM factor_values WHERE entity_type=? AND entity_id=?
                                   QUALIFY row_number() OVER (PARTITION BY metric ORDER BY snapshot_date DESC)=1""", [entity, entity_id]).fetchall():
        vals[m] = v
        meta[m] = json.loads(mt) if mt else None
    _scrub({**vals, "meta": meta})
    return {"factors": vals, "meta": meta}


@app.get("/api/keyword/{keyword}")
def keyword_detail(keyword: str, store: str = settings.store, entity: str = "keyword"):
    con = _con()
    try:
        if entity == "topic":
            members = [r[0] for r in con.execute("SELECT keyword FROM topic_keywords WHERE topic_id=? AND store=?", [keyword, store]).fetchall()]
            hist = con.execute("""SELECT metric, snapshot_date, value FROM factor_values WHERE entity_type='topic' AND entity_id=? ORDER BY 1,2""", [keyword]).fetchall()
            h: dict[str, list] = {}
            for m, d, v in hist:
                h.setdefault(m, []).append([str(d), v])
            return {"keyword": keyword, "members": members, "history": h, "apps": [], "play": [], "votes": [], **_full(con, keyword, "topic")}
        hist = con.execute("""SELECT metric, snapshot_date, value FROM factor_values WHERE entity_type='keyword' AND entity_id=?
                              ORDER BY metric, snapshot_date""", [keyword]).fetchall()
        apps = con.execute("""SELECT rank, app_id, name, developer, genre, rating_count, avg_rating, subtitle FROM search_results
                              WHERE store=? AND keyword=? AND snapshot_date=(SELECT max(snapshot_date) FROM search_results WHERE store=? AND keyword=?)
                              ORDER BY rank LIMIT 20""", [store, keyword, store, keyword]).fetchall()
        play = con.execute("""SELECT s.rank, s.package, s.title, a.min_installs, a.offers_iap, a.free, a.score FROM play_search s
                              LEFT JOIN (SELECT * FROM play_apps WHERE country=? QUALIFY row_number() OVER (PARTITION BY package ORDER BY snapshot_date DESC)=1) a USING (package)
                              WHERE s.country=? AND s.keyword=? AND s.snapshot_date=(SELECT max(snapshot_date) FROM play_search WHERE country=? AND keyword=?)
                              ORDER BY s.rank LIMIT 10""", [store, store, keyword, store, keyword]).fetchall()
        votes = con.execute("SELECT persona, score, rationale FROM llm_votes WHERE entity_id=? ORDER BY snapshot_date DESC LIMIT 10", [keyword]).fetchall()
        h: dict[str, list] = {}
        for m, d, v in hist:
            h.setdefault(m, []).append([str(d), v])
        return {"keyword": keyword, "history": h, **_full(con, keyword, "keyword"),
                "apps": [dict(zip(["rank", "app_id", "name", "developer", "genre", "rating_count", "avg_rating", "subtitle"], a)) for a in apps],
                "play": [dict(zip(["rank", "package", "title", "min_installs", "offers_iap", "free", "score"], p)) for p in play],
                "votes": [dict(zip(["persona", "score", "rationale"], v)) for v in votes]}
    finally:
        con.close()


@app.get("/api/own")
def own_apps(store: str = settings.store):
    """Your own apps (config/own_apps.toml): metadata terms with their ASO numbers, rank history per term, recent sales."""
    from ..pipeline.own import load_own_apps, own_terms, rank_history
    cfg_apps = load_own_apps()
    if not cfg_apps:
        return []
    con = _con()
    try:
        out = []
        for a in cfg_apps:
            terms = own_terms(a)
            kws = [t["term"] for t in terms]
            found = {}
            if kws:
                for k, vals, metas in con.execute("""
                    WITH f AS (SELECT entity_id, metric, value, meta FROM factor_values WHERE entity_type='keyword' AND entity_id IN (SELECT unnest(?))
                               QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1)
                    SELECT entity_id, map_from_entries(list((metric, value))), map_from_entries(list((metric, meta))) FROM f GROUP BY entity_id""", [kws]).fetchall():
                    vals, metas = dict(vals), dict(metas)
                    found[k] = {"keyword": k, **vals, "meta": {m: (json.loads(v) if v else None) for m, v in metas.items()}}
            hist = rank_history(con, store, a["app_id"], kws)
            ranks = {k: h[-1][1] for k, h in hist.items() if h and h[-1][1] is not None}
            ids = a.get("sku_app_ids") or [a["app_id"]]
            usd, dl = con.execute(f"""SELECT coalesce(sum(proceeds_usd),0), coalesce(sum(CASE WHEN kind='download' THEN units ELSE 0 END),0)
                                      FROM own_sales WHERE app_id IN ({','.join('?' * len(ids))}) AND report_date >= current_date - 90""", ids).fetchone()
            for t in terms:
                t["row"] = found.get(t["term"])
                t["rank"] = ranks.get(t["term"])
                h = hist.get(t["term"]) or []
                t["history"] = h
                t["rank_first"] = next((r for _, r in h if r is not None), None)
                t["best_rank"] = min((r for _, r in h if r is not None), default=None)
                ws = (found.get(t["term"]) or {}).get("weak_spots")
                t["open_spots"] = ws
            out.append({"name": a["name"], "app_id": a["app_id"], "title": a.get("title"), "subtitle": a.get("subtitle"), "keywords": a.get("keywords"),
                        "terms": terms, "ranks": ranks, "history": hist, "monthly_usd": round(usd / 3, 2), "monthly_downloads": int(dl / 3)})
        return out
    finally:
        con.close()


def _has(con, table):
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]).fetchone()[0] > 0


@app.get("/api/budget")
def budget_get():
    con = _con()
    try:
        cfg = budget.load()
        if not (_has(con, "spend") and _has(con, "warnings")):
            return {"caps": cfg.get("caps", {}), "cadence": cfg.get("cadence", {}), "unit_prices": cfg.get("unit_prices", {}),
                    "month_to_date": {"total": 0.0}, "spend": [], "warnings": []}
        caps = cfg.get("caps", {})
        mtd = {api: budget.month_to_date(con, api) for api in ("deepseek", "dataforseo", "scrapecreators")}
        mtd["total"] = budget.month_to_date(con)
        rows = con.execute("""SELECT api, job, date_trunc('day', ts) d, sum(usd), sum(units), count(*) FROM spend
                              WHERE ts >= date_trunc('month', current_date) GROUP BY 1,2,3 ORDER BY 3 DESC, 1""").fetchall()
        warns = con.execute("SELECT rowid, ts, kind, api, message FROM warnings WHERE NOT acknowledged ORDER BY ts DESC LIMIT 20").fetchall()
        return {"caps": caps, "cadence": cfg.get("cadence", {}), "unit_prices": cfg.get("unit_prices", {}), "month_to_date": mtd,
                "spend": [dict(zip(["api", "job", "day", "usd", "units", "calls"], (r[0], r[1], str(r[2])[:10], r[3], r[4], r[5]))) for r in rows],
                "warnings": [dict(zip(["id", "ts", "kind", "api", "message"], (w[0], str(w[1])[:16], w[2], w[3], w[4]))) for w in warns]}
    finally:
        con.close()


@app.put("/api/budget")
def budget_put(payload: dict = Body(...)):
    caps = {k: float(v) for k, v in (payload.get("caps") or {}).items() if k in ("deepseek", "dataforseo", "scrapecreators", "total")}
    cadence = {k: int(v) for k, v in (payload.get("cadence") or {}).items() if k in ("trends_every_days", "volume_every_days")}
    budget.save_caps(caps, cadence)
    return {"caps": budget.load()["caps"], "cadence": budget.load().get("cadence", {})}


@app.post("/api/warnings/{wid}/ack")
def warning_ack(wid: int):
    try:
        con = connect()
    except Exception as e:  # writer lock held by a collection run
        return JSONResponse({"ok": False, "error": f"database busy (collection running): {e.__class__.__name__}"}, status_code=423)
    try:
        con.execute("UPDATE warnings SET acknowledged=true WHERE rowid=?", [wid])
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/plan")
def plan_get(store: str = settings.store):
    from ..pipeline.weekly import run_plan
    con = _con()
    try:
        return run_plan(con, store)
    finally:
        con.close()


# ---------------------------------------------------------------- research
@app.post("/api/research")
def research_start(payload: dict = Body(...)):
    """Start a deep research run in a separate process (DuckDB allows one writer; the dashboard stays read-only)."""

    from ..pipeline.research import new_run
    topic = (payload.get("topic") or "").strip()
    if len(topic) < 2:
        return JSONResponse({"error": "type a keyword or topic"}, status_code=400)
    aspects = [a for a in (payload.get("aspects") or []) if isinstance(a, dict) and a.get("name")]
    run = new_run(topic, (payload.get("app") or "").strip() or None, payload.get("depth") or "deep", aspects,
                  reddit=True)
    log_path = settings.db_path.parent.parent / "logs" / f"research_{run['id']}.log"
    _spawn(["research", "--run-id", run["id"]], log_path)
    return run


_ASPECT_JOBS: dict[str, dict] = {}


@app.post("/api/research/aspects")
def research_aspects(payload: dict = Body(...)):
    """Step 1 of a research run: grounded aspects. Collects real evidence (App Store autocomplete, tracked searches,
    competitors, 4-5 star reviews, Reddit titles), then DeepSeek groups it (~2-4 min). Returns a job id to poll.
    grounded=false gives the old quick brainstorm (~30 s) synchronously."""
    import threading
    import uuid

    from ..sources.deepseek import DeepSeekClient
    topic = (payload.get("topic") or "").strip()
    app_desc = (payload.get("app") or "").strip() or None
    if len(topic) < 2:
        return JSONResponse({"error": "type a keyword or topic"}, status_code=400)
    if not DeepSeekClient().available:
        return JSONResponse({"error": "DeepSeek key missing: research runs without aspects"}, status_code=400)
    if payload.get("grounded") is False:
        from .. import budget
        from ..pipeline.discover import write_retry
        ds = DeepSeekClient()
        try:
            out = ds.aspects(topic, app_desc)
        except Exception as e:
            return JSONResponse({"error": f"DeepSeek failed: {e}"}, status_code=502)
        try:
            write_retry(lambda c: budget.record(c, "deepseek", "aspects", ds.usage_in + ds.usage_out, ds.cost_usd(), topic), tries=3, wait=1)
        except Exception:
            pass
        return {"topic": topic, "aspects": out}
    jid = uuid.uuid4().hex[:8]
    job = _ASPECT_JOBS[jid] = {"id": jid, "topic": topic, "status": "running", "step": "starting", "pct": 0}

    def work():
        from ..pipeline.ground import ground_aspects
        try:
            job["result"] = ground_aspects(topic, app_desc, progress=lambda n, p: job.update(step=n, pct=p))
            job["status"] = "done"
        except Exception as e:
            log.exception("grounded aspects")
            job["status"], job["error"] = "error", str(e)
    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


@app.get("/api/research/aspects/{jid}")
def research_aspects_job(jid: str):
    job = _ASPECT_JOBS.get(jid)
    return job if job else JSONResponse({"error": "unknown job (server restarted?)"}, status_code=404)


@app.get("/api/research")
def research_list():
    from ..pipeline.research import list_runs
    return list_runs()


@app.delete("/api/research/{rid}")
def research_delete(rid: str):
    """Delete a saved research report (the keywords it added stay in the universe)."""
    import re as _re

    from ..pipeline.research import RUNS
    if not _re.fullmatch(r"[0-9a-f]{6,32}", rid):
        return JSONResponse({"error": "bad id"}, status_code=400)
    p = RUNS / f"{rid}.json"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    import json as _j
    if _j.loads(p.read_text()).get("status") == "running":
        return JSONResponse({"error": "still running"}, status_code=409)
    p.unlink()
    return {"deleted": rid}


@app.post("/api/research/{rid}/verdict")
def research_verdict(rid: str):
    """Compute (or recompute) a saved report's aspect verdicts and overall call in a separate process."""
    import re as _re

    from ..pipeline.research import RUNS, load_run
    if not _re.fullmatch(r"[0-9a-f]{6,32}", rid) or not load_run(rid):
        return JSONResponse({"error": "not found"}, status_code=404)
    run = load_run(rid)
    run["step"] = "verdict"
    (RUNS / f"{rid}.json").write_text(json.dumps(run, default=str))
    log_path = settings.db_path.parent.parent / "logs" / f"verdict_{rid}.log"
    _spawn(["research-verdict", rid], log_path)
    return {"started": rid}


@app.get("/api/research/{rid}")
def research_get(rid: str):
    from ..pipeline.research import load_run
    r = load_run(rid)
    return r if r else JSONResponse({"error": "not found"}, status_code=404)


# ---------------------------------------------------------------- manual weekly run
def _weekly_status() -> dict:
    p = settings.db_path.parent / "cache" / "weekly_status.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _weekly_warnings() -> list[str]:
    """Data would be barely fresher if run now: last full run < 7 days ago, or Apple's new popularity week
    (Sun-Sat) is normally published by Wednesday."""
    warn, st, now = [], _weekly_status(), dt.datetime.now()
    if st.get("state") == "running":
        warn.append(f"A weekly run is already running (started {st.get('started_at')}).")
    last = st.get("last_full_run")
    if last:
        days = (now - dt.datetime.fromisoformat(last)).total_seconds() / 86400
        if days < 7:
            warn.append(f"The last full run was {days:.1f} days ago; most sources change weekly, so results will barely move.")
    else:
        warn.append("No previous manual full run recorded (first run: expect several hours).")
    if now.weekday() in (6, 0, 1):   # Sun, Mon, Tue
        warn.append("Apple publishes last week's popularity around Wednesday; running before then reuses the old week.")
    return warn


def _light_status() -> dict:
    p = settings.db_path.parent / "cache" / "light_status.json"
    return json.loads(p.read_text()) if p.exists() else {}


@app.get("/api/run/light")
def light_get():
    st = _light_status()
    warn = []
    if st.get("state") == "running":
        warn.append(f"A light update is already running (started {st.get('started_at')}).")
    if st.get("last_light_run"):
        hours = (dt.datetime.now() - dt.datetime.fromisoformat(st["last_light_run"])).total_seconds() / 3600
        if hours < 20:
            warn.append(f"The last light update was {hours:.0f} hours ago; rating counts barely move within a day.")
    return {"status": st, "warnings": warn}


@app.post("/api/run/light")
def light_start(payload: dict = Body(default={})):
    r = light_get()
    if r["status"].get("state") == "running":
        return JSONResponse({"started": False, "warnings": r["warnings"]}, status_code=409)
    if r["warnings"] and not payload.get("force"):
        return {"started": False, "warnings": r["warnings"]}
    log_path = settings.db_path.parent.parent / "logs" / f"light_{dt.datetime.now():%Y%m%d_%H%M}.log"
    _spawn(["collect", "light"], log_path)
    return {"started": True, "log": str(log_path)}


@app.get("/api/run/weekly")
def weekly_get():
    from ..pipeline.weekly import run_plan
    con = _con()
    try:
        plan = run_plan(con, settings.store)
    finally:
        con.close()
    return {"status": _weekly_status(), "warnings": _weekly_warnings(), "plan": plan}


@app.post("/api/run/weekly")
def weekly_start(payload: dict = Body(default={})):
    warn = _weekly_warnings()
    if _weekly_status().get("state") == "running":
        return JSONResponse({"started": False, "warnings": warn}, status_code=409)
    if warn and not payload.get("force"):
        return {"started": False, "warnings": warn}
    log_path = settings.db_path.parent.parent / "logs" / f"weekly_{dt.datetime.now():%Y%m%d_%H%M}.log"
    _spawn(["run-weekly"], log_path)
    return {"started": True, "log": str(log_path)}
