"""Command line: apptopics <command> / apptopics <group> <command>. Thin wrappers over pipeline/*."""
from __future__ import annotations

import json
import logging

import typer

from .config import settings
from .db import connect, connect_read

app = typer.Typer(help="AppTopicsQuants: rank app topics like a quant ranks stocks.", no_args_is_help=True)
collect = typer.Typer(help="Collect snapshots into DuckDB.", no_args_is_help=True)
llm = typer.Typer(help="DeepSeek jobs.", no_args_is_help=True)
anchors = typer.Typer(help="Real revenue anchors and curve fitting.", no_args_is_help=True)
dfs = typer.Typer(help="DataForSEO jobs (Google volume, Trends, Play rankings and reviews).", no_args_is_help=True)
budget_cli = typer.Typer(help="Spend caps and month-to-date spend.", no_args_is_help=True)
for name, t in (("collect", collect), ("llm", llm), ("anchors", anchors), ("budget", budget_cli), ("dfs", dfs)):
    app.add_typer(t, name=name)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
S = settings.store


# ---------------------------------------------------------------- top level
@app.command()
def init():
    """Create the database schema."""
    connect().close()
    typer.echo(f"ok: {settings.db_path}")


@app.command()
def doctor():
    """Check every source: connectivity for the free ones, configuration for the keyed ones."""
    from .sources.appleads import AppleAdsClient
    from .sources.asc import ASCClient
    from .sources.astro import AstroClient
    from .sources.dataforseo import DataForSEOClient
    from .sources.deepseek import DeepSeekClient
    from .sources.googleplay import PlayClient
    from .sources.itunes import ITunesClient
    for label, fn in (("astro", lambda: f"{AstroClient().connect().get('version')} ({len(AstroClient().list_tools())} tools)"),
                      ("itunes", lambda: f"grossing/all -> {len(ITunesClient().chart('grossing', None))} entries"),
                      ("google play", lambda: f"search('hiit timer') -> {len(PlayClient().search('hiit timer', num=5))} results")):
        try:
            typer.echo(f"{label}: {fn()}")
        except Exception as e:
            typer.echo(f"{label}: FAIL {e}")
    for label, client, hint in (("deepseek", DeepSeekClient(), "DEEPSEEK_API_KEY"), ("dataforseo", DataForSEOClient(), "DATAFORSEO_LOGIN/PASSWORD"),
                                ("apple ads platform api", AppleAdsClient(), "APPLE_ADS_*"), ("app store connect", ASCClient(), "ASC_*")):
        typer.echo(f"{label}: {'configured' if client.available else 'not configured (' + hint + ')'}")


@app.command()
def plan(store: str = S):
    """Estimate the next full run's paid-API spend against month-to-date and caps (no calls are made)."""
    from .pipeline.weekly import run_plan
    con, _ = connect_read()
    with con:
        rows = run_plan(con, store)
    typer.echo(f"{'api':11} {'job':18} {'items':>6} {'est $':>7} {'mtd $':>7} {'cap $':>7}  ok")
    for r in rows:
        typer.echo(f"{r['api']:11} {r['job']:18} {r['items']:>6} {r['est_usd']:>7.3f} {r['mtd_usd']:>7.2f} {r['cap_usd'] or '-'!s:>7}  "
                   f"{'yes' if r['ok'] else 'NO: ' + r['reason']}")


@app.command()
def score(store: str = S, only: list[str] = typer.Option(None)):
    """Run all metric plugins, topic rollups, composites, the opportunity/ASO index and the dashboard copy."""
    from .pipeline.index import build_index
    from .pipeline.score import composite, run_metrics, score_all
    with connect() as con:
        if only:   # partial: recompute the named metrics, then everything downstream
            typer.echo(run_metrics(con, store, only=only))
            from .pipeline.topics import build_topic_index
            typer.echo(f"composite keyword: {composite(con, store, None, 'keyword')}, index: {build_index(con, store, None)}, "
                       f"topics: {build_topic_index(con, store, None)}")
            from .db import snapshot_copy
            snapshot_copy(con)
        else:
            typer.echo(score_all(con, store))


@app.command()
def top(n: int = typer.Option(30, "-n", "--n"), store: str = S, entity: str = "keyword",
        all: bool = typer.Option(False, "--all", help="include excluded (concentrated/brand) entries")):
    """Print the top-N keywords (or --entity topic) by composite."""
    from .db import today
    from .pipeline.score import factor_matrix
    con, _ = connect_read()
    with con:
        comp = {k: (v, json.loads(m) if m else {}) for k, v, m in con.execute("""SELECT entity_id, value, meta FROM factor_values
            WHERE metric='composite' AND entity_type=? QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""",
            [entity]).fetchall()}
        mat = factor_matrix(con, store, today(), entity)
    rows = sorted(((k, v, mat.get(k, {})) for k, (v, m) in comp.items() if all or not m.get("excluded")), key=lambda r: -r[1])
    cols = [("breadth", "brdth", ".2f"), ("attainable", "attn", ".2f"), ("concentration", "conc", ".2f"), ("entrant_share", "new", ".2f"),
            ("indie_share", "ind", ".2f"), ("demand", "dem", ".0f"), ("trending", "trnd", ".1f"), ("competition", "cmp", ".2f"),
            ("difficulty", "dif", ".0f"), ("brandness", "brand", ".2f")]
    typer.echo(f"{'keyword':30} {'comp':>6} " + " ".join(f"{h:>5}" for _, h, _ in cols))
    for k, v, f in rows[:n]:
        typer.echo(f"{k[:30]:30} {v:6.2f} " + " ".join(f"{(format(f[m], fmt) if m in f else '-'):>5}" for m, _, fmt in cols))


@app.command("research-reddit")
def research_reddit(run_id: str = typer.Argument(..., help="research run id"),
                    credits: int = typer.Option(100, help="hard cap on Reddit credits (1 per request)"),
                    video: bool = typer.Option(True, "--video/--no-video", help="TikTok + YouTube (~8 credits per aspect)"),
                    ads: bool = typer.Option(True, "--ads/--no-ads", help="Meta Ad Library for the top 12 competitors (~24 credits)")):
    """Add Reddit, TikTok, YouTube and Meta ads (ScrapeCreators) to a finished research run; one merged voice of users."""
    from .pipeline.reddit_voice import add_reddit
    typer.echo(add_reddit(run_id, credits, video=video, ads=ads))


@app.command("social-topics")
def social_topics(topics: str = typer.Option(None, "--topics", help="comma-separated topic ids"),
                  grades: str = typer.Option(None, "--grades", help="instead of ids: topics whose grade is in this set, e.g. ABC"),
                  limit: int = typer.Option(100, help="with --grades: at most this many topics, best first"),
                  video: bool = typer.Option(True, "--video/--no-video", help="TikTok + YouTube search per topic (2 credits)"),
                  ads: bool = typer.Option(True, "--ads/--no-ads", help="Meta Ad Library for the topic's top apps (2 credits per app)"),
                  ads_per_topic: int = typer.Option(5, help="apps checked per topic")):
    """TikTok/YouTube views and Meta ad activity per TOPIC (ScrapeCreators) -> topic_social, for the Opportunities factors."""
    from .pipeline.social_topics import collect_topics, topics_by_grade
    if topics:
        ids = [t.strip() for t in topics.split(",") if t.strip()]
    elif grades:
        con, _ = connect_read()
        with con:
            ids = topics_by_grade(con, grades, limit)
    else:
        raise typer.BadParameter("give --topics or --grades")
    est = ((2 if video else 0) + (2 * ads_per_topic if ads else 0)) * len(ids)
    typer.echo(f"{len(ids)} topics, up to {est} credits")
    r = collect_topics(ids, video=video, ads=ads, ads_per_topic=ads_per_topic)
    typer.echo(f"{'topic':34} {'tt vids':>7} {'tt views':>10} {'yt vids':>7} {'yt views':>10} {'apps':>4} {'ads':>3} {'ver':>3} {'max':>4} {'days':>4} {'cr':>3}")
    for t in r["topics"]:
        f = lambda v: "-" if v is None else f"{v:,}"
        typer.echo(f"{(t.get('label') or t['topic_id'])[:34]:34} {f(t['tiktok_videos']):>7} {f(t['tiktok_views']):>10} {f(t['youtube_videos']):>7} "
                   f"{f(t['youtube_views']):>10} {f(t['apps_checked']):>4} {f(t['apps_with_ads']):>3} {f(t['apps_verified_ads']):>3} "
                   f"{f(t['max_active_ads']):>4} {f(t['max_oldest_active_days']):>4} {f(t['credits']):>3}")
    typer.echo(f"credits used {r['credits_used']} (${r['usd']:.3f}), remaining {r.get('credits_remaining')}")


@app.command("research-verdict")
def research_verdict(run_id: str = typer.Argument(..., help="research run id"),
                     rescore: bool = typer.Option(True, "--rescore/--no-rescore", help="recompute social factors + index first")):
    """Aspect verdicts and the overall call for a finished research run (also feeds its social evidence into the model)."""
    from .pipeline.verdict import finalize
    v = finalize(run_id, rescore=rescore)
    typer.echo(f"{v.get('grade')} · {v.get('call') or ''}\n{v.get('next_step') or ''}")


@app.command("research-market")
def research_market(run_id: str = typer.Argument(..., help="research run id (see the Research tab)")):
    """Rebuild only the market report of a finished research run (competitors, prices, feature map, reviews, highlights)."""
    from .pipeline.research import rebuild_market
    m = rebuild_market(run_id)
    typer.echo({"aspects": [(a["aspect"], a["band"]) for a in m["aspects"]], "competitors": len(m["competitors"]),
                "off_topic": [o["name"] for o in m.get("off_topic", [])], "prices": m["prices"].get("suggested")})


@app.command()
def research(topic: str = typer.Argument("", help="keyword or topic, e.g. 'pet health'"),
             app_desc: str = typer.Option(None, "--app", help="optional: your app's name + one-line description, for metadata advice"),
             depth: str = typer.Option("deep", help="deep (~10-30 min) | quick (~2-5 min)"),
             reddit: bool = typer.Option(True, "--reddit/--no-reddit", help="Reddit (ScrapeCreators, ~16 credits per aspect, max 80)"),
             run_id: str = typer.Option(None, help="internal: resume a run created by the dashboard")):
    """Deep research on a topic: expand (DeepSeek) -> autocomplete -> popularity -> competitors -> score -> report."""
    import json as _j

    from .pipeline.research import load_run, new_run, run_research
    run = load_run(run_id) if run_id else new_run(topic, app_desc, depth, reddit=reddit)
    if run is None:
        raise typer.BadParameter(f"unknown run {run_id}")
    run = run_research(run)
    r = run.get("result") or {}
    typer.echo(_j.dumps({"id": run["id"], "status": run["status"], "found": r.get("found"), "scored": r.get("scored"),
                         "build_now": [i["keyword"] for i in r.get("build_now", [])][:10],
                         "top_aso": [i["keyword"] for i in r.get("top_aso", [])][:10]}, indent=1))


@app.command("backtest")
def backtest_cmd(horizon: int = 28):
    """Did higher opportunity grades predict newcomer entry / popularity / band revenue? (needs >= horizon days of history)"""
    import json as _j

    from .db import connect_read_live
    from .pipeline.backtest import backtest
    con = connect_read_live()
    try:
        typer.echo(_j.dumps(backtest(con, horizon), indent=1, default=str))
    finally:
        con.close()


@app.command("calibrate-anchors")
def calibrate_anchors(refresh: bool = False, holdout: bool = typer.Option(False, "--holdout", help="honest test: refit on 2/3 of the anchors, score the other 1/3"),
                      apply: bool = typer.Option(False, "--apply", help="write the refitted curve into config/weights.toml")):
    """Fit the revenue estimator on verified-revenue apps (TrustMRR + MobileMRR caches) and print the error report."""
    import json as _j

    from .pipeline.anchor_fit import calibrate, fit
    from .sources.revenue_dirs import trustmrr_mobile
    if refresh:
        trustmrr_mobile(refresh=True)
    with connect() as con:
        r = calibrate(con, apply=apply) if (holdout or apply) else fit(con)
    typer.echo(_j.dumps(r, indent=1, default=str))


@app.command()
def validate(store: str = S):
    """Check the ranking against config/golden.toml and the top-grossing chart (see pipeline/validate.py)."""
    import json as _j

    from .db import connect_read
    from .pipeline.validate import validate as _v
    con, _ = connect_read()
    try:
        typer.echo(_j.dumps(_v(con, store), indent=1, default=str))
    finally:
        con.close()


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765):
    """Run the local dashboard."""
    import uvicorn
    typer.echo(f"dashboard: http://{host}:{port}/")
    uvicorn.run("apptopics.web.app:app", host=host, port=port, reload=False)


# ---------------------------------------------------------------- collect
@collect.command("all")
def c_all(top: int = 300, store: str = S, play: bool = True, difficulty: bool = True, play_top: int = 150,
          llm: bool = True, external: bool = True, reviews_top: int = 150, social_top: int = 300):
    """Weekly full run: every source that has credentials, then score keywords and topics."""
    from .pipeline.weekly import run_full
    with connect() as con:
        typer.echo(run_full(con, store, top, play_top, reviews_top, social_top, play, difficulty, llm, external))


@app.command("run-weekly")
def run_weekly(store: str = S):
    """Manual weekly full run (dashboard button). Records status in data/cache/weekly_status.json."""
    import datetime as _dt
    import json as _j

    from .pipeline.weekly import run_full
    from .sources.base import machine_lock
    st = settings.db_path.parent / "cache" / "weekly_status.json"
    st.parent.mkdir(parents=True, exist_ok=True)
    prev = _j.loads(st.read_text()) if st.exists() else {}
    with machine_lock("weekly"):
        st.write_text(_j.dumps({**prev, "state": "running", "started_at": _dt.datetime.now().isoformat(timespec="seconds")}))
        try:
            with connect() as con:
                res = run_full(con, store)
            st.write_text(_j.dumps({"state": "done", "started_at": _j.loads(st.read_text())["started_at"],
                                    "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
                                    "last_full_run": _dt.datetime.now().isoformat(timespec="seconds"), "result": str(res)[:2000]}))
        except Exception as e:
            st.write_text(_j.dumps({**prev, "state": "error", "error": repr(e)[:500], "finished_at": _dt.datetime.now().isoformat(timespec="seconds")}))
            raise


@collect.command("light")
def c_light(top: int = 1500, store: str = S):
    """Daily light run (free, ~5 min): your own apps' result pages (rank history), charts, fresh rating counts for the
    band apps of the top-N keywords (rating velocity series)."""
    from .pipeline.own import refresh_own_pages
    from .pipeline.weekly import run_light
    n_own = refresh_own_pages(store)
    with connect() as con:
        typer.echo({"own_pages": n_own, **run_light(con, store, top)})


@collect.command("keywords")
def c_keywords(store: str = S, max_pages: int = typer.Option(None)):
    """Snapshot Astro's most-searched keyword list (the universe)."""
    from .pipeline.collect import collect_keywords
    with connect() as con:
        typer.echo(collect_keywords(con, store, max_pages))


@collect.command("popularity")
def c_popularity(weeks: int = 26, store: str = S):
    """Apple Ads Platform API: weekly search-term popularity for the last N weeks (trending + universe)."""
    from .pipeline.collect import collect_popularity, keywords_from_popularity
    with connect() as con:
        typer.echo(collect_popularity(con, store, weeks))
        typer.echo(f"universe from Apple Ads: {keywords_from_popularity(con, store)}")


@collect.command("charts")
def c_charts(store: str = S):
    """Apple top grossing/free/paid charts for every genre."""
    from .pipeline.collect import collect_charts
    with connect() as con:
        typer.echo(collect_charts(con, store))


@collect.command("search")
def c_search(top: int = typer.Option(None), store: str = S, no_brand_filter: bool = False, only_new: bool = False,
             backend: str = typer.Option(None, help="auto|dataforseo|itunes|astro (default from config [sources])")):
    """App Store search pages (full sectors) for the top-N keywords. --only-new skips keywords that already have a page."""
    from .pipeline.collect import collect_search_auto, selected
    with connect() as con:
        top = top or settings.weights().get("sources", {}).get("search_top", 300)
        kws = selected(con, store, top, not no_brand_filter)
        if only_new:
            have = {r[0] for r in con.execute("SELECT DISTINCT keyword FROM search_results WHERE store=?", [store]).fetchall()}
            kws = [k for k in kws if k not in have]
        typer.echo(f"{len(kws)} keywords")
        typer.echo(collect_search_auto(con, kws, store, backend))


@collect.command("apps")
def c_apps(store: str = S, limit: int = typer.Option(None)):
    """iTunes lookup for every app seen in search pages and charts."""
    from .pipeline.collect import collect_apps
    with connect() as con:
        typer.echo(collect_apps(con, store, limit=limit))


@collect.command("difficulty")
def c_difficulty(top: int = 300, store: str = S):
    """Track top-N keywords under a placeholder app in Astro to obtain difficulty."""
    from .pipeline.collect import collect_difficulty, selected
    with connect() as con:
        typer.echo(collect_difficulty(con, selected(con, store, top), store))


@collect.command("play")
def c_play(top: int = 150, store: str = S, num: int = 20, details_top: int = 10):
    """Google Play search pages + app details (real install counts) for top-N keywords."""
    from .pipeline.collect import collect_play, selected
    with connect() as con:
        typer.echo(collect_play(con, selected(con, store, top), store, num, details_top))


@collect.command("play-charts")
def c_play_charts(store: str = S):
    """Google Play grossing/free/paid top-100 for every category."""
    from .pipeline.collect import collect_play_charts
    with connect() as con:
        typer.echo(collect_play_charts(con, store))


@collect.command("reviews")
def c_reviews(top: int = 150, store: str = S, pages: int = 2):
    """Recent Apple reviews for the p5-p20 band apps of the top-N keywords by composite."""
    from .pipeline.collect import band_app_ids, collect_reviews, ranked
    with connect() as con:
        ids = band_app_ids(con, store, ranked(con, top))
        typer.echo(f"{len(ids)} band apps")
        typer.echo(collect_reviews(con, ids, store, pages))


@collect.command("volume")
def c_volume(top: int = 1500, store: str = S):
    """Google Ads search volume + CPC via DataForSEO."""
    from .pipeline.collect import collect_volume, selected
    with connect() as con:
        typer.echo(collect_volume(con, selected(con, store, top), store))


@collect.command("trends")
def c_trends(top: int = 1000, store: str = S, reference: str = "calendar app"):
    """Google Trends (5y, vs a reference term) via DataForSEO for the top-N by composite."""
    from .pipeline.collect import collect_trends, ranked
    with connect() as con:
        typer.echo(collect_trends(con, ranked(con, top), store, reference))


@collect.command("longtail")
def c_longtail(store: str = S, seeds: int = typer.Option(None, help="limit seeds (default: all 'need' keywords)"),
               per_seed: int = 6, hints: bool = True, llm_variants: bool = True):
    """Discover long-tail searches from 'need' keywords: App Store autocomplete + DeepSeek variants."""
    from .pipeline.collect import collect_longtail
    typer.echo(collect_longtail(store, seeds, per_seed, hints, llm_variants))


@collect.command("longtail-finish")
def c_longtail_finish(store: str = S, min_pop: int = 15, max_search: int = typer.Option(None)):
    """Label -> Astro popularity -> iTunes competitor pages -> rescore, for terms found by `collect longtail`."""
    from .pipeline.collect import finish_longtail
    typer.echo(finish_longtail(store, min_pop, max_search))


@collect.command("asc")
def c_asc(days: int = 90):
    """Your own apps' daily sales reports from App Store Connect."""
    from .pipeline.collect import collect_asc
    with connect() as con:
        typer.echo(collect_asc(con, days))


# ---------------------------------------------------------------- llm
@llm.command("labels")
def l_labels(store: str = S, relabel: bool = False, workers: int = 8):
    """Label ALL universe keywords need/brand/vague/rubbish (brand+rubbish are hidden from the ranking and paid fetches)."""
    from .pipeline.collect import collect_llm_labels
    typer.echo(collect_llm_labels(store, relabel, workers))


@llm.command("brands")
def l_brands(top: int = 1500, store: str = S):
    """Classify top-N keywords as brand vs generic."""
    from .pipeline.collect import collect_llm_brands, selected
    with connect() as con:
        typer.echo(collect_llm_brands(con, selected(con, store, top, brand_filter=False), store))


@llm.command("buildability")
def l_build(top: int = 1500, store: str = S):
    """Score top-N keywords for buildability under [constraints] in config/weights.toml."""
    from .pipeline.collect import collect_llm_buildability, selected
    with connect() as con:
        typer.echo(collect_llm_buildability(con, selected(con, store, top), store))


@llm.command("cluster")
def l_cluster(store: str = S):
    """Group every scoreable keyword into product topics (DeepSeek, ~$0.2), then score the topics."""
    from .pipeline.discover import write_retry
    from .pipeline.topics import build_topic_index, cluster_universe
    typer.echo(cluster_universe(store))
    typer.echo({"topics_scored": write_retry(lambda c: build_topic_index(c, store))})
    from .db import snapshot_copy
    write_retry(snapshot_copy)


@llm.command("attach-hidden")
def l_attach_hidden(store: str = S, min_pop: float = 5):
    """Put hidden keywords (brands, games, thin sectors...) into topics too, for the dashboard's Show hidden (~$0.5)."""
    from .pipeline.discover import write_retry
    from .pipeline.topics import attach_hidden
    typer.echo(attach_hidden(store, min_pop=min_pop))
    from .db import snapshot_copy
    write_retry(snapshot_copy)


@llm.command("merge-topics")
def l_merge_topics(store: str = S):
    """Second merge pass over existing topics: near-duplicate labels become one topic (no re-clustering)."""
    from .pipeline.discover import write_retry
    from .pipeline.topics import merge_topics
    typer.echo(merge_topics(store))
    from .db import snapshot_copy
    write_retry(snapshot_copy)


@llm.command("pain-top")
def l_pain_top(grades: str = "AB", store: str = S):
    """Pain points for the top-graded niches: band-app reviews (iTunes) -> DeepSeek fixable complaints."""
    from .pipeline.discover import mine_pain_points
    typer.echo(mine_pain_points(store, tuple(grades)))


@llm.command("pain")
def l_pain(top: int = 100, store: str = S):
    """Mine negative reviews of band apps for fixable complaints (factor review_gap)."""
    from .pipeline.collect import collect_llm_pain_points, ranked
    with connect() as con:
        typer.echo(collect_llm_pain_points(con, ranked(con, top), store))


@llm.command("vote")
def l_vote(top: int = 50, store: str = S):
    """Simulated consumer panel on the top-N by composite."""
    from .pipeline.collect import collect_llm_votes, ranked
    with connect() as con:
        typer.echo(collect_llm_votes(con, ranked(con, top), store))


# ---------------------------------------------------------------- anchors
@anchors.command("import")
def a_import(path: str, source: str = typer.Option(None)):
    """Import anchors from CSV: app_id, monthly_revenue_usd[, monthly_downloads, source, note, observed_at]."""
    from pathlib import Path

    from .pipeline.anchor_fit import import_anchors
    with connect() as con:
        typer.echo(import_anchors(con, Path(path), source))


@anchors.command("list")
def a_list():
    con, _ = connect_read()
    with con:
        for r in con.execute("SELECT app_id, source, monthly_revenue_usd, monthly_downloads, observed_at, note FROM calibration_anchors ORDER BY observed_at DESC").fetchall():
            typer.echo(r)


# ---------------------------------------------------------------- budget
@budget_cli.command("show")
def b_show():
    from . import budget
    con, _ = connect_read()
    with con:
        caps = budget.load().get("caps", {})
        for api in ("deepseek", "dataforseo", "scrapecreators", "total"):
            typer.echo(f"{api:11} mtd ${budget.month_to_date(con, None if api == 'total' else api):7.2f}  cap ${caps.get(api, 0):7.2f}")
        if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='warnings'").fetchone()[0]:
            for w in con.execute("SELECT ts, api, message FROM warnings WHERE NOT acknowledged ORDER BY ts DESC LIMIT 10").fetchall():
                typer.echo(f"  warning {w[0]:%Y-%m-%d %H:%M} [{w[1]}] {w[2]}")


@budget_cli.command("set")
def b_set(api: str, usd: float):
    """Set a monthly cap: apptopics budget set deepseek 20"""
    from . import budget
    budget.save_caps({api: usd})
    typer.echo(budget.load()["caps"])


@dfs.command("volume")
def d_volume(store: str = S):
    """Google search volume + CPC for every Google-safe keyword without recent data."""
    from .pipeline.dfs import volume_all
    typer.echo(volume_all(store))


@dfs.command("trends")
def d_trends(n: int = 1000, store: str = S):
    """Google Trends (5 years) for the top-N keywords: growth + seasonality."""
    from .pipeline.dfs import trends_top
    typer.echo(trends_top(store, n))


@dfs.command("play")
def d_play(n: int = 300, store: str = S):
    """Google Play rankings for the top-N keywords."""
    from .pipeline.dfs import play_rankings
    typer.echo(play_rankings(store, n))


@dfs.command("play-reviews")
def d_play_reviews(grades: str = "AB", per_kw: int = 8, no_fetch: bool = False, store: str = S):
    """Google Play reviews of the apps ranking for Build-now niches -> DeepSeek fixable complaints (play_review_gap)."""
    from .pipeline.dfs import play_pain_points
    typer.echo(play_pain_points(store, tuple(grades), per_kw, fetch=not no_fetch))


if __name__ == "__main__":
    app()
