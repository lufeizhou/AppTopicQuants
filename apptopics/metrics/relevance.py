"""Does an app in a keyword's results actually serve that keyword?

Apple fills the results for brand or vague searches ('me@walmart', 'dia') with unrelated big apps.
Counting those as the keyword's competitors makes a navigational query look like a broad, rich,
unconcentrated market. An app is *relevant* when its name + description start of text contain the
keyword's meaningful words (word forms match: tracker~tracking, translator~translate, running~run):
all of them for a one-word keyword; for longer ones at least half AND the keyword's *anchor* word.

Anchor (Step 2 review, 2026-11-06): a word Apple itself treats as required, read from its top 10 results.
Words in >= 8 of the top-10 names: the rarest of them ('calorie' in 'calorie counter', not 'counter'); if no
word is that common, the most common one ('timer' in 'timer widget', 'blocker' in 'app blocker for
productivity'). Generic words ('ai', 'easy') are never the anchor of a longer search ('mirror' in 'ai mirror'). Before, the anchor was simply the rarest word, which is
often a describing word ('widget', 'animated', 'productivity') and threw out real competitors.

Read-as (typos, other languages): when at most 2 of Apple's top 10 contain the keyword's words but the
page is full, Apple read the search as something else ('camra' -> camera, 'despertador' -> alarm). The
keyword is then matched on the word most top-10 names share (>= 4 of them) and READ_AS records it.
Only for searches labelled 'need' with p_brand < 0.5 (a brand search keeps its own words).

DeepSeek hybrid (Step 2, 2026-11-06): app_relevance holds DeepSeek's names-only answer for every (search, app) pair
(thinking off: collect_llm_relevance). Where it agrees with the word rule, that is the answer; where they disagree
(~28% of pairs), DeepSeek's answer with 150 characters of description decides (relevant_desc,
collect_llm_relevance_desc). On 200 hand-labelled random pairs: hybrid 80% right, names only 75%, word rule 76%,
descriptions for every app 83%. Apps not judged yet use the word rule.
Overrides, strongest first: relevant_manual (2,400 pairs judged by hand), relevant_sonnet (one-off Sonnet review of
~69k earning disputed apps), p_jev (TypeSafe Jev on every app, >= JEV_THRESHOLD; 85% right on 200 random pairs vs
75% DeepSeek names), then the DeepSeek / word-rule hybrid above.
"""
from __future__ import annotations

import re
from collections import Counter

STOP = {"app", "apps", "free", "the", "a", "an", "for", "and", "of", "to", "my", "best", "new", "online", "pro",
        "plus", "with", "in", "on", "by", "me", "you", "your", "i", "from", "into", "without", "no", "or", "at",
        "is", "it", "that", "this", "get", "all", "using", "via", "as"}
NOT_A_JOB = STOP | {"ai", "hd", "lite", "official", "simple", "easy", "maker", "&", "-"}   # never a read-as word
SUFFIXES = ("ations", "ation", "ions", "ion", "ings", "ing", "ers", "ors", "er", "or", "ies", "es", "ed", "s", "e")
DOUBLING = {"ings", "ing", "ers", "er", "ed"}   # running -> runn -> run, scanner -> scann -> scan
JEV_THRESHOLD = 0.5            # Jev p(real option) cut-off; 0.5 = 85% / 74% right on the random / hard hand-labelled sets
READ_AS: dict[str, str] = {}   # keyword -> the word Apple read it as (last relevance() call)
JUDGED: set[str] = set()       # keywords whose apps were judged by DeepSeek (app_relevance), last relevance() call


def stem(t: str) -> str:
    for suf in SUFFIXES:
        if len(t) - len(suf) >= 4 and t.endswith(suf) and not (suf == "s" and t.endswith("ss")):   # chess, fitness
            t = t[:-len(suf)]
            if suf in DOUBLING and t[-1] == t[-2] and t[-1] not in "aeiou":
                t = t[:-1]
            break
    return t


def _pattern(token: str) -> str:
    s = stem(token)
    return r"\b" + re.escape(s) + (r"s?\b" if len(token) <= 3 else "")   # exact word for 'ai', 'pdf', 'gif(s)'


def tokens(keyword: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", keyword.lower()) if t not in STOP]


def patterns(keyword: str) -> list[str]:
    """Word-start regexes for the keyword's meaningful tokens."""
    return [_pattern(t) for t in tokens(keyword)]


def threshold(n_patterns: int) -> float:
    return 1.0 if n_patterns <= 1 else 0.5


def anchor(keyword: str, top_names: list[str], df) -> int | None:
    """Index of the keyword's anchor pattern (multi-word keywords only): a word Apple itself treats as required.
    Candidates skip generic words ('ai', 'easy'). Words in >= 80% of Apple's top-10 names are required by Apple:
    the rarest of them ('calorie' in 'calorie counter'). If none is, the word most top-10 names carry ('timer' in
    'timer widget': 7 of 10 say timer, 5 say widget, so Apple shows timers without 'widget')."""
    toks = tokens(keyword)
    if len(toks) <= 1:
        return None
    idx = [i for i, t in enumerate(toks) if t not in NOT_A_JOB] or list(range(len(toks)))
    cnt = {i: sum(1 for n in top_names if re.search(_pattern(toks[i]), n)) for i in idx}
    need = max(1, round(0.8 * len(top_names)))
    strong = [i for i in idx if cnt[i] >= need]
    if strong:
        return min(strong, key=lambda i: df(stem(toks[i])))
    return min(idx, key=lambda i: (-cnt[i], df(stem(toks[i]))))


def read_as(top_names: list[str]) -> str | None:
    """The word most of Apple's top-10 names share (>= 4 of them), for a search Apple read as something else.
    Counted by stem ('camera'/'cameras'); returns the commonest spelling."""
    words = [w for n in top_names for w in set(re.findall(r"[a-z]+", n)) if w not in NOT_A_JOB and len(w) >= 3]
    c = Counter(stem(w) for w in words)
    if not c:
        return None
    s, k = c.most_common(1)[0]
    return Counter(w for w in words if stem(w) == s).most_common(1)[0][0] if k >= 4 else None


def relevance(con, store: str, search_date, use_llm: bool = True) -> dict[tuple[str, str], float]:
    """(keyword, app_id) -> share of keyword patterns found in the app's name/subtitle/description[:2000];
    0 when a multi-word keyword's anchor pattern is missing."""
    import bisect
    top: dict[str, list[str]] = {}
    for k, n in con.execute("SELECT keyword, lower(coalesce(name,'')||' '||coalesce(subtitle,'')) FROM sr_latest "
                            "WHERE store=? AND snapshot_date=? AND rank<=10 ORDER BY keyword, rank", [store, search_date]).fetchall():
        top.setdefault(k, []).append(n)
    kws = [k for (k,) in con.execute("SELECT DISTINCT keyword FROM sr_latest WHERE store=? AND snapshot_date=?",
                                     [store, search_date]).fetchall()]
    # document frequency of each word: share of distinct app names+subtitles with a word starting with its stem
    names = [t for (t,) in con.execute("SELECT DISTINCT lower(coalesce(name,'')||' '||coalesce(subtitle,'')) FROM sr_latest "
                                       "WHERE store=? AND snapshot_date=?", [store, search_date]).fetchall()]
    wc = Counter(w for t in names for w in set(re.findall(r"[a-z0-9]+", t)))
    words = sorted(wc)
    cum = [0]
    for w in words:
        cum.append(cum[-1] + wc[w])

    def df_of(stem_: str) -> int:   # upper bound (a name with two such words counts twice); fine for ranking words
        i = bisect.bisect_left(words, stem_)
        j = bisect.bisect_left(words, stem_ + "￿")
        return cum[j] - cum[i]

    def run(spec: dict[str, tuple[list[str], str | None]]):
        con.execute("CREATE OR REPLACE TEMP TABLE _kp (keyword VARCHAR, pats VARCHAR[], rare VARCHAR)")
        con.executemany("INSERT INTO _kp VALUES (?, ?, ?)", [(k, p, a) for k, (p, a) in spec.items()])
        rows = con.execute("""
            WITH a AS (SELECT app_id, left(description, 2000) d FROM apps WHERE store=?
                       QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1),
                 x AS (SELECT s.keyword, s.app_id, s.rank, k.pats, k.rare,
                              lower(coalesce(s.name,'')||' '||coalesce(s.subtitle,'')||' '||coalesce(a.d,'')) txt
                       FROM sr_latest s JOIN _kp k USING (keyword) LEFT JOIN a USING (app_id)
                       WHERE s.store=? AND s.snapshot_date=?)
            SELECT keyword, app_id, rank,
                   CASE WHEN len(pats)=0 THEN 1.0
                        WHEN rare IS NOT NULL AND NOT regexp_matches(txt, rare) THEN 0.0
                        ELSE len(list_filter(pats, lambda p: regexp_matches(txt, p))) / len(pats) END
            FROM x""", [store, store, search_date]).fetchall()
        con.execute("DROP TABLE IF EXISTS _kp")
        return rows

    spec = {}
    for k in kws:
        p = patterns(k)
        i = anchor(k, top.get(k, []), df_of)
        spec[k] = (p, p[i] if i is not None else None)
    rows = run(spec)
    out = {(k, a): float(r) for k, a, _, r in rows}
    # read-as: Apple's top 10 hardly contain the keyword's words although the page is full
    hits, size = Counter(), Counter()
    for k, _, rank, r in rows:
        size[k] += 1
        if rank <= 10 and r >= threshold(len(spec[k][0])):
            hits[k] += 1
    READ_AS.clear()
    redo = {}
    try:   # only searches labelled as a need (not 'netflix' read as 'movie')
        needs = {k for (k,) in con.execute("SELECT keyword FROM keyword_labels WHERE store=? AND label='need' AND coalesce(p_brand, 1) < 0.5",
                                           [store]).fetchall()}
    except Exception:
        needs = set()
    for k in kws:
        if k in needs and spec[k][0] and hits[k] <= 2 and size[k] >= 30:
            w = read_as(top.get(k, []))
            if w and not any(re.match(p, w) for p in spec[k][0]):
                READ_AS[k] = w
                redo[k] = ([r"\b" + re.escape(stem(w))], None)
    if redo:
        for k, a, _, r in run(redo):
            out[(k, a)] = float(r)
    # Hybrid with DeepSeek (app_relevance): where the word rule and DeepSeek's names-only answer agree, that is the
    # answer; where they disagree, DeepSeek's answer with descriptions decides (relevant_desc), else the names answer.
    JUDGED.clear()
    if not use_llm:
        return out
    cols = {c for (c,) in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name='app_relevance'").fetchall()}
    if not cols:        # table not created yet
        return out
    extra = ", r.relevant_manual" if "relevant_manual" in cols else ", NULL"
    extra += ", r.relevant_sonnet" if "relevant_sonnet" in cols else ", NULL"
    extra += ", r.p_jev" if "p_jev" in cols else ", NULL"
    extra += ", r.relevant_desc" if "relevant_desc" in cols else ", NULL"   # older copies lack the column: never fall back silently
    rows = con.execute(f"""SELECT r.keyword, r.app_id, r.relevant{extra} FROM app_relevance r
            JOIN sr_latest s ON s.keyword=r.keyword AND s.app_id=r.app_id AND s.store=r.store
            WHERE r.store=? AND s.snapshot_date=?""", [store, search_date]).fetchall()
    for k, a, r, man, son, pj, rd in rows:
        rule = out.get((k, a), 0.0) >= threshold(len(spec[k][0]) if k in spec else 1)
        if man is not None:      # reviewed by hand (one-off review 2026-11-06): always wins
            keep = bool(man)
        elif son is not None:    # one-off Sonnet review of earning disputed apps (2026-11-06)
            keep = bool(son)
        elif pj is not None:     # TypeSafe Jev (2026-11-06): the base judge for every app on every page
            keep = pj >= JEV_THRESHOLD
        else:
            keep = rule if bool(r) == rule else (rd if rd is not None else r)
        out[(k, a)] = 1.0 if keep else 0.0
        JUDGED.add(k)
    return out
