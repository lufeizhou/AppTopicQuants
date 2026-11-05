"""Step 1 universe expansion: find searches we do not track yet, from sources that produce store-style phrases.

  graded seeds   needs (specificity >= 3) whose opp grade is in [step1.expand].seed_grades
  hints          App Store autocomplete of graded seeds not used as seeds before (+ 'seed a'..'seed z' for
                 hint_alphabet_grades)
  play           Google Play autocomplete of graded seeds (free, unofficial endpoint)
  titles         2-3 word phrases from the names/subtitles of apps on good keywords' results pages, kept when
                 >= title_min_apps different apps use them ('Seconds: HIIT Tabata Timer' -> 'hiit timer', 'tabata timer')
  dfs_play_app   PILOT (paid): keywords that competitor apps of A/B searches rank for on Google Play (DataForSEO Labs)
  reddit         PILOT (paid): 'is there an app' / 'app for' posts per A/B topic -> DeepSeek turns them into searches

Every hit is stored in expand_finds (keyword, source, seed), so each source's yield can be judged later (pilot rule:
keep a paid source if > 10% of its new terms grade C or better). New terms go into longtail_keywords + the current
keywords snapshot; `finish` labels them, gets Astro popularity, fetches results pages for popularity >=
[step1.universe].fetch_min_popularity, relabels with the page's top apps, rescores and attaches them to topics.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import Counter, defaultdict

from ..config import settings
from ..db import connect_read_live, today, upsert
from .collect import is_latin

log = logging.getLogger("apptopics.expand")

STOP = {"a", "an", "and", "the", "for", "with", "to", "of", "in", "on", "by", "your", "my", "you", "our", "at", "from", "or",
        "is", "it", "&", "app", "apps", "free", "pro", "plus", "lite", "best", "new", "all", "one", "no", "be", "get", "more",
        "edition", "version", "hd", "official", "inc", "llc", "ltd", "co"}
_SPLIT = re.compile(r"[:\-–—|,+/()\[\]!?.•·]+|\s&\s")
_TOK = re.compile(r"[a-z][a-z0-9']*")


def _cfg() -> dict:
    return settings.section("expand")


DONE_PATH = settings.db_path.parent / "cache" / "expand_done.json"


def done_recent(kind: str, days: int | None = None) -> set[str]:
    """Inputs (alphabet seeds, DFS packages) expanded within redo_after_days: skipped, so a rerun pays only for the rest."""
    days = int(_cfg().get("redo_after_days", 28)) if days is None else days
    d = json.loads(DONE_PATH.read_text()) if DONE_PATH.exists() else {}
    cut = (today() - dt.timedelta(days=days)).isoformat()
    return {k for k, when in d.get(kind, {}).items() if when >= cut}


def mark_done(kind: str, items) -> None:
    d = json.loads(DONE_PATH.read_text()) if DONE_PATH.exists() else {}
    d.setdefault(kind, {}).update({k: today().isoformat() for k in items})
    DONE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DONE_PATH.write_text(json.dumps(d, indent=0, sort_keys=True))


def _known(con, store: str) -> set[str]:
    known = {k for (k,) in con.execute("SELECT DISTINCT keyword FROM keywords WHERE store=?", [store]).fetchall()}
    known |= {k for (k,) in con.execute("SELECT keyword FROM longtail_keywords WHERE store=?", [store]).fetchall()}
    return known


def _ok(term: str) -> bool:
    excl = [t.lower() for t in settings.section("filters").get("exclude_terms", [])]
    # ':' and ' - ' mark an app title (App Store hints suggest app names: 'sleep calculator: bedtime'); people do not
    # type them and Astro rejects them
    return (bool(term) and is_latin(term) and 1 <= len(term.split()) <= 6 and len(term) <= 45 and ":" not in term
            and " - " not in term and not any(t in term for t in excl))


def graded_needs(con, grades: str, visible_only: bool = False) -> list[str]:
    """Needs (specificity >= 3, or not yet scored for specificity) with a latest opp grade in `grades`, best first."""
    rows = con.execute("""
        WITH o AS (SELECT entity_id k, value v, json_extract_string(meta, '$.grade') g, json_extract_string(meta, '$.action') a
                   FROM factor_values WHERE metric='opp_score' AND entity_type='keyword'
                   QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC, computed_at DESC)=1)
        SELECT o.k, o.a FROM o JOIN keyword_labels l ON l.keyword=o.k
        WHERE l.label='need' AND coalesce(l.specificity, 3) >= 3 AND o.g IN (SELECT unnest(?)) ORDER BY o.v DESC""",
        [list(grades)]).fetchall()
    return [k for k, a in rows if is_latin(k) and (not visible_only or a in ("Build now", "Maybe"))]


# -- free sources --------------------------------------------------------------------------------------------
class Throttled(RuntimeError):
    """Apple keeps answering 429: stop the source (what was saved stays saved; rerun later resumes)."""


def from_hints(seeds: list[str], alphabet_seeds: list[str], known: set[str], save=None, chunk: int = 300) -> dict[str, dict]:
    """App Store autocomplete. Plain seeds first, then 'seed a'..'seed z'. Every `chunk` queries the finds are handed
    to save(found, finished_alphabet_seeds), so a stop loses at most one chunk; an alphabet seed counts as done only
    when all 26 letters were answered. 429 -> wait 1, 2, 4, 8 min and retry; still 429 -> Throttled."""
    import time

    import httpx

    from ..sources.base import RateLimiter
    from ..sources.itunes import ITunesClient
    cfg = _cfg()
    it = ITunesClient()
    it.limiter = RateLimiter(float(cfg.get("hints_per_minute", 40)), shared="itunes_hints")
    queries = [(s, s, False) for s in seeds] + [(s, f"{s} {ch}", True) for s in alphabet_seeds for ch in "abcdefghijklmnopqrstuvwxyz"]
    found: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    letters: Counter = Counter()
    failed: set[str] = set()
    done_alpha: list[str] = []

    def flush():
        nonlocal pending, done_alpha
        if save and (pending or done_alpha):
            save(pending, done_alpha)
        pending, done_alpha = {}, []

    for i, (seed, q, is_alpha) in enumerate(queries, 1):
        for attempt in range(5):
            try:
                for h in it.hints(q):
                    if h != seed and h not in known and h not in found and _ok(h):
                        found[h] = pending[h] = {"seed": seed, "source": "hints", "letter": q[-1] if is_alpha else None}
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    log.warning("hints %r failed: %s", q, e)
                    if is_alpha:
                        failed.add(seed)
                    break
                if attempt == 4:
                    flush()
                    raise Throttled(f"App Store hints still 429 after backoff at query {i}/{len(queries)}") from e
                log.warning("hints: 429 at %d/%d, waiting %d min", i, len(queries), 2 ** attempt)
                time.sleep(60 * 2 ** attempt)
            except Exception as e:
                log.warning("hints %r failed: %s", q, e)
                if is_alpha:
                    failed.add(seed)
                break
        if is_alpha:
            letters[seed] += 1
            if letters[seed] == 26 and seed not in failed:
                done_alpha.append(seed)
        if i % chunk == 0:
            flush()
        if i % 200 == 0:
            log.info("hints %d/%d queries, %d new", i, len(queries), len(found))
    flush()
    return found


def from_play(seeds: list[str], known: set[str]) -> dict[str, dict]:
    from ..sources.base import RateLimiter
    from ..sources.googleplay import PlayClient
    p = PlayClient(limiter=RateLimiter(90, shared="play_suggest"))
    found: dict[str, dict] = {}
    for i, seed in enumerate(seeds, 1):
        try:
            for h in p.suggest(seed):
                if h != seed and h not in known and h not in found and _ok(h):
                    found[h] = {"seed": seed, "source": "play_suggest"}
        except Exception as e:
            log.warning("play suggest %r failed: %s", seed, e)
        if i % 200 == 0:
            log.info("play suggest %d/%d seeds, %d new", i, len(seeds), len(found))
    return found


def title_phrases(names: list[tuple[str, str | None, str | None]]) -> dict[str, set[str]]:
    """[(app_id, name, subtitle)] -> phrase -> set of app ids using it. Phrases are 2-3 consecutive words inside one
    segment of the title (segments split at ':', '-', '|', ',' ...), not starting or ending with a stop word."""
    out: dict[str, set[str]] = defaultdict(set)
    for app_id, name, sub in names:
        for text in (name or "", sub or ""):
            for seg in _SPLIT.split(text.lower()):
                toks = _TOK.findall(seg)
                for n in (2, 3):
                    for i in range(len(toks) - n + 1):
                        g = toks[i:i + n]
                        if g[0] in STOP or g[-1] in STOP or any(len(t) < 2 for t in g):
                            continue
                        out[" ".join(g)].add(app_id)
    return out


def from_titles(con, keywords: list[str], known: set[str], top: int, min_apps: int, max_new: int) -> dict[str, dict]:
    rows = con.execute("""SELECT s.keyword, s.app_id, s.name, s.subtitle FROM search_results s WHERE s.keyword IN (SELECT unnest(?)) AND s.rank <= ?
                          QUALIFY dense_rank() OVER (PARTITION BY s.keyword ORDER BY s.snapshot_date DESC)=1""", [keywords, top]).fetchall()
    apps = {a: (n, sub) for _k, a, n, sub in rows}
    first_kw: dict[str, str] = {}
    for k, a, _n, _s in rows:
        first_kw.setdefault(a, k)
    phrases = title_phrases([(a, n, sub) for a, (n, sub) in apps.items()])
    cand = sorted(((p, ids) for p, ids in phrases.items() if len(ids) >= min_apps and p not in known and _ok(p)), key=lambda x: -len(x[1]))
    return {p: {"seed": first_kw.get(next(iter(ids))), "source": "titles", "apps": len(ids)} for p, ids in cand[:max_new]}


# -- paid pilots ---------------------------------------------------------------------------------------------
def dfs_packages(con, n_apps: int = 50) -> list[tuple]:
    """Top Play apps (rank <= 5) of our visible A/B searches: [(package, best rank, keyword)]."""
    ab = graded_needs(con, "AB", visible_only=True)
    pk = con.execute("""SELECT package, min(rank) r, any_value(keyword) FROM play_search WHERE keyword IN (SELECT unnest(?)) AND rank <= 5
                        GROUP BY package ORDER BY r, package""", [ab]).fetchall()
    order = {k: i for i, k in enumerate(ab)}
    done = done_recent("dfs_packages")
    return [r for r in sorted(pk, key=lambda r: (r[1], order.get(r[2], 1e9))) if r[0] not in done][:n_apps]


def from_dfs_play_apps(pk: list[tuple], known: set[str], per_app: int = 200, max_usd: float = 2.0) -> tuple[dict[str, dict], float]:
    """Keywords that the given Play apps rank for (DataForSEO Labs keywords_for_app, US/en). No DB connection is held."""
    from ..sources.dataforseo import DataForSEOClient
    c = DataForSEOClient()
    found: dict[str, dict] = {}
    for i, (package, _r, kw) in enumerate(pk, 1):
        if c.spent_usd >= max_usd:
            log.warning("dfs play pilot: stopped at $%.2f", c.spent_usd)
            break
        try:
            res = c._post("/dataforseo_labs/google/keywords_for_app/live",
                          [{"app_id": package, "location_code": 2840, "language_code": "en", "limit": per_app}])
        except Exception as e:
            log.warning("keywords_for_app %s failed: %s", package, e)
            continue
        mark_done("dfs_packages", [package])
        for r in res:
            for it in r.get("items") or []:
                kd = it.get("keyword_data") or {}
                t = str(kd.get("keyword") or "").strip().lower()
                if t and t not in known and t not in found and _ok(t):
                    found[t] = {"seed": kw, "source": "dfs_play_app", "volume": (kd.get("keyword_info") or {}).get("search_volume"),
                                "app": package}
        if i % 10 == 0:
            log.info("dfs play pilot %d/%d apps, %d new, $%.2f", i, len(pk), len(found), c.spent_usd)
    return found, c.spent_usd


REDDIT_SYS = ("You read Reddit posts in which people ask for or discuss apps. For each post that expresses a need an app "
              "could serve, write the App Store search phrases that person would type to find such an app: 1-5 words, "
              "lowercase, generic (no brand or app names), the way people really search ('baby sleep tracker', 'split "
              "bills with roommates'). Skip posts with no app need. At most 3 phrases per post. Respond as JSON: "
              "{\"items\": [{\"post\": int, \"searches\": [str]}]}")


def reddit_topics(con, n_topics: int = 100) -> list[str]:
    """Labels of the best Build-now topics."""
    return [lab for (lab,) in con.execute("""
        SELECT t.label FROM factor_values f JOIN topics t ON t.topic_id=f.entity_id
        WHERE f.metric='opp_score' AND f.entity_type='topic' AND json_extract_string(f.meta, '$.action')='Build now'
        QUALIFY row_number() OVER (PARTITION BY f.entity_id ORDER BY f.snapshot_date DESC, f.computed_at DESC)=1
        ORDER BY f.value DESC LIMIT ?""", [n_topics]).fetchall()]


def from_reddit(topics: list[str], known: set[str], max_credits: int = 400) -> tuple[dict[str, dict], float]:
    """Reddit posts asking for apps around the given topics -> DeepSeek -> search phrases. No usernames are read or kept.
    No DB connection is held."""
    from ..sources.deepseek import DeepSeekClient
    from ..sources.scrapecreators import USD_PER_CREDIT, CreditCap, ScrapeCreators
    sc = ScrapeCreators(max_credits=max_credits)
    posts: list[dict] = []
    seen: set[str] = set()
    try:
        for i, lab in enumerate(topics, 1):
            for q in (f'"is there an app" {lab}', f'"app for" {lab}'):
                try:
                    r = sc.search(q)
                except CreditCap:
                    raise
                except Exception as e:
                    log.warning("reddit search %r: %s", q, e)
                    continue
                for p in r.get("posts") or []:
                    pid = p.get("id") or p.get("name")
                    if pid and pid not in seen:
                        seen.add(pid)
                        posts.append({"topic": lab, "title": (p.get("title") or "")[:200], "body": (p.get("selftext") or "")[:500]})
            if i % 20 == 0:
                log.info("reddit pilot %d/%d topics, %d posts", i, len(topics), len(posts))
    except CreditCap as e:
        log.warning("reddit pilot stopped: %s", e)
    usd = sc.used * USD_PER_CREDIT
    ds = DeepSeekClient()
    found: dict[str, dict] = {}
    for i in range(0, len(posts), 40):
        chunk = posts[i:i + 40]
        try:
            data = ds.json_chat(REDDIT_SYS, json.dumps([{"post": j, "title": p["title"], "body": p["body"]} for j, p in enumerate(chunk)]),
                                temperature=0.0)
        except Exception as e:
            log.warning("reddit extraction failed: %s", e)
            continue
        for it in data.get("items", []):
            j = it.get("post")
            topic = chunk[j]["topic"] if isinstance(j, int) and 0 <= j < len(chunk) else None
            for t in it.get("searches") or []:
                t = str(t).strip().lower()
                if t and t not in known and t not in found and _ok(t):
                    found[t] = {"seed": topic, "source": "reddit"}
    return found, usd + ds.cost_usd()


# -- orchestration -------------------------------------------------------------------------------------------
def save_finds(all_found: dict[str, dict[str, dict]], store: str = settings.store) -> dict:
    """all_found: source -> term -> info. Writes expand_finds (every hit) and adds new terms to the universe."""
    from .discover import write_retry
    d, now = today(), dt.datetime.now()
    finds = [{"store": store, "keyword": t, "source": src, "seed": v.get("seed"), "info": json.dumps({k: x for k, x in v.items() if k not in ("seed", "source")}) or None,
              "found_at": now} for src, f in all_found.items() for t, v in f.items()]
    first: dict[str, dict] = {}
    for src, f in all_found.items():
        for t, v in f.items():
            first.setdefault(t, {"seed": v.get("seed"), "source": src})

    def fn(c):
        snap = c.execute("SELECT max(snapshot_date) FROM keywords WHERE store=?", [store]).fetchone()[0] or d
        known = _known(c, store)
        new = [t for t in first if t not in known]
        upsert(c, "expand_finds", finds)
        upsert(c, "longtail_keywords", [{"store": store, "keyword": t, "seed": first[t]["seed"], "source": first[t]["source"], "hint_rank": None,
                                         "popularity": None, "discovered_at": d} for t in new])
        upsert(c, "keywords", [{"store": store, "keyword": t, "genre": None, "popularity": None, "rank_in_store": None, "snapshot_date": snap} for t in new])
        return new
    new = write_retry(fn)
    return {"new_terms": len(new), "by_source": dict(Counter(first[t]["source"] for t in new)), "hits_by_source": {s: len(f) for s, f in all_found.items()}}


def collect(store: str = settings.store, sources: tuple[str, ...] = ("hints", "play", "titles"), pilots: tuple[str, ...] = (),
            seeds_limit: int | None = None, alpha_grades: str | None = None) -> dict:
    """Each source is saved (and its spend recorded) as soon as it finishes, so a later failure loses nothing paid for."""
    from concurrent.futures import ThreadPoolExecutor

    from .. import budget
    from .discover import write_retry
    cfg = _cfg()
    con = connect_read_live()
    try:
        known = _known(con, store)
        seeds = graded_needs(con, cfg.get("seed_grades", "AB"))[: seeds_limit or int(cfg.get("seeds_max", 4000))]
        used = {s for (s,) in con.execute("SELECT DISTINCT seed FROM longtail_keywords WHERE store=? AND source='hints' AND seed IS NOT NULL", [store]).fetchall()}
        done_alpha = done_recent("alphabet_seeds")
        alpha = [s for s in graded_needs(con, alpha_grades or cfg.get("hint_alphabet_grades", "A"), visible_only=True)
                 if s not in done_alpha][: seeds_limit or int(cfg.get("alpha_max_seeds", 150))]
        title_kws = graded_needs(con, cfg.get("title_grades", "AB"))
    finally:
        con.close()
    results: dict[str, dict] = {}

    def keep(src: str, found: dict, api: str | None = None, usd: float = 0.0):
        r = save_finds({src: found}, store)
        if api and usd:
            write_retry(lambda c: budget.record(c, api, f"expand_{src}_pilot", 0, usd, f"{len(found)} terms"))
        results[src] = {"hits": len(found), "new": r["new_terms"], **({"usd": round(usd, 3)} if usd else {})}
        log.info("%s: %s", src, results[src])

    # read connections are closed before saving: DuckDB refuses a write connection while this process holds a read one
    if "titles" in sources:
        con = connect_read_live()
        try:
            f = from_titles(con, title_kws, known, int(cfg.get("title_top_apps", 30)), int(cfg.get("title_min_apps", 3)),
                            int(cfg.get("title_max_new", 3000)))
        finally:
            con.close()
        keep("titles", f)
    for pilot in pilots:
        con = connect_read_live()          # read the pilot's inputs, then release the DB before any network call
        try:
            inputs = dfs_packages(con, int(cfg.get("dfs_apps", 50))) if pilot == "dfs_play_app" else reddit_topics(con) if pilot == "reddit" else None
        finally:
            con.close()
        if pilot == "dfs_play_app":
            f, usd = from_dfs_play_apps(inputs, known, max_usd=float(cfg.get("dfs_max_usd", 2.0)))
            keep(pilot, f, "dataforseo", usd)
        elif pilot == "reddit":
            f, usd = from_reddit(inputs, known)
            keep(pilot, f, "scrapecreators", usd)
    with ThreadPoolExecutor(2) as ex:      # different hosts: run App Store and Play autocomplete side by side
        hits = {"hits": 0, "new": 0}

        def save_hints(found: dict, done: list[str]):
            r = save_finds({"hints": found}, store) if found else {"new_terms": 0}
            mark_done("alphabet_seeds", done)
            hits["hits"] += len(found)
            hits["new"] += r["new_terms"]
            results["hints"] = dict(hits)

        fh = ex.submit(from_hints, [s for s in seeds if s not in used], alpha, known, save_hints) if "hints" in sources else None
        fp = ex.submit(from_play, seeds, known) if "play" in sources and cfg.get("play_suggest", True) else None
        if fh:
            try:
                fh.result()
            except Throttled as e:
                log.warning("%s; saved so far: %s", e, hits)
                results["hints"] = {**hits, "stopped": str(e)}
            log.info("hints: %s", results.get("hints"))
        if fp:
            keep("play_suggest", fp.result())
    return {"sources": results, "seeds": len(seeds), "alphabet_seeds": len(alpha)}


def judge_buildability(store: str, terms: list[str] | None = None) -> int:
    """Grade A needs a judged buildability; judge every search that passes the other three A checks (money, newcomers,
    traffic) and has none (DeepSeek, cents). All keywords, not only `terms`: rescoring also lifts old searches."""
    from .collect import collect_llm_buildability
    from .discover import write_retry
    con = connect_read_live()
    try:
        todo = [k for (k,) in con.execute("""SELECT entity_id FROM factor_values WHERE metric='opp_score' AND entity_type='keyword'
                QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC, computed_at DESC)=1
                AND json_extract_string(meta,'$.checks.money')='true' AND json_extract_string(meta,'$.checks.newcomers')='true'
                AND json_extract_string(meta,'$.checks.traffic')='true'
                AND entity_id NOT IN (SELECT entity_id FROM factor_values WHERE metric='buildability')""").fetchall()]
    finally:
        con.close()
    log.info("buildability: %d A candidates to judge", len(todo))
    return write_retry(lambda c: collect_llm_buildability(c, todo, store)) if todo else 0


# third-party utilities for a brand's device are an open indie market (user, 2026-11-05): never relabelled brand
DEVICE_COMPANION = re.compile(r"\b(remote|remotes|tv app|tv cast|screen mirror\w*|chromecast|fire ?stick|roku|smart tv)\b")


def brand_check(store: str = settings.store, grades: tuple[str, ...] = ("A", "B"), redo: bool = False) -> dict:
    """Second label pass (2026-11-05): needs graded `grades` whose name is a company, device, franchise or app become
    label 'brand' (p_brand >= [step1.labels] brand_check_min_p). Every answer is kept as metric llm_brand_check, so a
    search is checked once unless redo."""
    from .collect import _factor_rows, _llm_job
    from .discover import write_retry
    lb = settings.section("labels")
    min_p = float(lb.get("brand_check_min_p", 0.7))
    con = connect_read_live()
    try:
        todo = [k for (k,) in con.execute(f"""SELECT o.entity_id FROM factor_values o JOIN keyword_labels l ON l.keyword=o.entity_id AND l.store=? AND l.label='need'
                WHERE o.metric='opp_score' AND o.entity_type='keyword'
                QUALIFY row_number() OVER (PARTITION BY o.entity_id ORDER BY o.snapshot_date DESC, o.computed_at DESC)=1
                AND json_extract_string(o.meta,'$.grade') IN (SELECT unnest(?))
                {"" if redo else "AND o.entity_id NOT IN (SELECT entity_id FROM factor_values WHERE metric='llm_brand_check')"}""",
                [store, list(grades)]).fetchall()]
        apps = {}
        for k, n in con.execute("""SELECT keyword, name FROM search_results WHERE store=? AND keyword IN (SELECT unnest(?)) AND rank<=3
                QUALIFY dense_rank() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1 ORDER BY keyword, rank""", [store, todo]).fetchall():
            apps.setdefault(k, []).append(n)
    finally:
        con.close()
    log.info("brand check: %d searches", len(todo))
    if not todo:
        return {"checked": 0, "brands": []}
    res: dict[str, dict] = {}

    def fn(ds):
        res.update(ds.brand_check([{"term": k, "apps": apps.get(k, [])} for k in todo]))
        return len(res)
    brands = []

    def write(c):
        n = _llm_job(c, "llm_brand_check", store, len(todo), fn)
        got = {k: v for k, v in res.items() if k in set(todo)}
        upsert(c, "factor_values", _factor_rows("llm_brand_check", {k: v["p_brand"] for k, v in got.items()}, got))
        for k, v in got.items():
            if v["p_brand"] >= min_p and not DEVICE_COMPANION.search(k):
                brands.append(k)
                c.execute("UPDATE keyword_labels SET label='brand', p_brand=?, reason=? WHERE store=? AND keyword=?",
                          [v["p_brand"], f"brand check: {v['kind']} - {v['reason']}"[:300], store, k])
        return n
    write_retry(write)
    return {"checked": len(todo), "answered": len(res), "brands": brands}


def finish(store: str = settings.store, since: dt.date | None = None, rescore: bool = True, attach: bool = True,
           max_per_source: int | None = None) -> dict:
    """Label -> Astro popularity -> results pages (>= fetch_min_popularity) -> relabel with context -> rescore -> attach to
    topics, for the terms expand added (discovered on/after `since`, default today). max_per_source measures a random
    sample per source (enough to judge a pilot's yield); the rest stay in the universe for a later run."""
    from .collect import collect_llm_labels
    from .discover import astro_popularity, label_terms, search_terms, write_retry
    since = since or today()
    min_pop = float(settings.section("universe").get("fetch_min_popularity", 15))
    con = connect_read_live()
    try:
        if max_per_source:
            terms = [k for (k,) in con.execute("""SELECT keyword FROM longtail_keywords WHERE store=? AND discovered_at>=?
                     QUALIFY row_number() OVER (PARTITION BY source ORDER BY hash(keyword))<=?""", [store, since, max_per_source]).fetchall()]
        else:
            terms = [k for (k,) in con.execute("SELECT keyword FROM longtail_keywords WHERE store=? AND discovered_at>=?", [store, since]).fetchall()]
    finally:
        con.close()
    labels = label_terms(terms, store)
    need = [t for t in terms if labels.get(t, {}).get("label") == "need"]
    log.info("expand finish: %d terms, %d need", len(terms), len(need))
    con = connect_read_live()
    try:
        have = {k: p for k, p in con.execute("""SELECT keyword, popularity FROM keyword_metrics WHERE popularity IS NOT NULL
                AND keyword IN (SELECT unnest(?)) QUALIFY row_number() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1""", [need]).fetchall()}
        searched = {k for (k,) in con.execute("SELECT DISTINCT keyword FROM search_results WHERE keyword IN (SELECT unnest(?))", [need]).fetchall()}
    finally:
        con.close()
    todo = [t for t in need if t not in have]
    for i in range(0, len(todo), 500):
        got = astro_popularity(todo[i:i + 500], store)
        have.update({k: v["popularity"] for k, v in got.items() if v.get("popularity") is not None})
        log.info("astro popularity: %d/%d", min(i + 500, len(todo)), len(todo))
    to_search = sorted([t for t in need if (have.get(t) or 0) >= min_pop and t not in searched], key=lambda t: -have[t])
    log.info("searching %d new terms with popularity >= %.0f", len(to_search), min_pop)
    n = search_terms(to_search, store, progress=lambda i, t: log.info("itunes search %d/%d", i, t) if i % 50 == 0 else None)
    relab = collect_llm_labels(store, scope="context")
    out = {"terms": len(terms), "need": len(need), "with_popularity": len(have), "searched": len(to_search), "rows": n,
           "relabelled": relab.get("new")}
    if rescore:
        from .score import score_all
        out["index"] = write_retry(lambda c: score_all(c, store)).get("index")
        out["buildability"] = 0
        for _ in range(3):                 # a rescore can make more searches A candidates: judge until none is left
            n = judge_buildability(store)
            if not n:
                break
            out["buildability"] += n
            out["index"] = write_retry(lambda c: score_all(c, store)).get("index")
    if attach:
        from .topics import attach_hidden
        out["attach"] = attach_hidden(store, only_hidden=False, terms_in=terms)
    return out
