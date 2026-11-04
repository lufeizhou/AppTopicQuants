"""Does an app in a keyword's results actually serve that keyword?

Apple fills the results for brand or vague searches ('me@walmart', 'dia') with unrelated big apps.
Counting those as the keyword's competitors makes a navigational query look like a broad, rich,
unconcentrated market. An app is *relevant* when its name + subtitle + description start of text
contain the keyword's meaningful words (light stemming: tracker~tracking, scanner~scan):
all of them for a one-word keyword; for longer ones at least half AND the keyword's rarest word
(lowest share of app names containing it): 'mirror' in 'ai mirror', 'calorie' in 'calorie counter'.
Otherwise a generic word ('ai', 'free', 'photo') would let unrelated apps in.
"""
from __future__ import annotations

import re

STOP = {"app", "apps", "free", "the", "a", "an", "for", "and", "of", "to", "my", "best", "new", "online", "pro",
        "plus", "with", "in", "on", "by", "me", "you", "your", "i"}


def stem(t: str) -> str:
    for suf in ("ers", "er", "ing", "es", "s"):
        if len(t) - len(suf) >= 4 and t.endswith(suf):
            t = t[:-len(suf)]
            break
    if len(t) >= 5 and t[-1] == t[-2]:
        t = t[:-1]   # scann -> scan
    return t


def patterns(keyword: str) -> list[str]:
    """Word-start regexes for the keyword's meaningful tokens (exact word for <= 3 letters: 'ai', 'pdf')."""
    out = []
    for t in re.findall(r"[a-z0-9]+", keyword.lower()):
        if t in STOP:
            continue
        s = stem(t)
        out.append(r"\b" + re.escape(s) + (r"\b" if len(s) <= 3 else ""))
    return out


def threshold(n_patterns: int) -> float:
    return 1.0 if n_patterns <= 1 else 0.5


def relevance(con, store: str, search_date) -> dict[tuple[str, str], float]:
    """(keyword, app_id) -> share of keyword patterns found in the app's name/subtitle/description[:2000];
    0 when a multi-word keyword's rarest pattern is missing."""
    kws = [k for (k,) in con.execute("SELECT DISTINCT keyword FROM sr_latest WHERE store=? AND snapshot_date=?",
                                     [store, search_date]).fetchall()]
    pats = {k: patterns(k) for k in kws}
    # document frequency of each keyword word: share of distinct app names+subtitles with a word starting with its stem
    import bisect
    from collections import Counter
    names = [t for (t,) in con.execute("SELECT DISTINCT lower(coalesce(name,'')||' '||coalesce(subtitle,'')) FROM sr_latest "
                                       "WHERE store=? AND snapshot_date=?", [store, search_date]).fetchall()]
    wc = Counter(w for t in names for w in set(re.findall(r"[a-z0-9]+", t)))
    words = sorted(wc)
    cum = [0]
    for w in words:
        cum.append(cum[-1] + wc[w])

    def df_of(stem_: str) -> int:   # upper bound (a name with two such words counts twice); fine for ranking words
        i = bisect.bisect_left(words, stem_)
        j = bisect.bisect_left(words, stem_ + "\uffff")
        return cum[j] - cum[i]
    stems = {k: [stem(t) for t in re.findall(r"[a-z0-9]+", k.lower()) if t not in STOP] for k in kws}
    con.execute("CREATE OR REPLACE TEMP TABLE _kp (keyword VARCHAR, pats VARCHAR[], rare VARCHAR)")
    con.executemany("INSERT INTO _kp VALUES (?, ?, ?)",
                    [(k, v, (v[min(range(len(v)), key=lambda i: df_of(stems[k][i]))] if len(v) > 1 else None)) for k, v in pats.items()])
    rows = con.execute("""
        WITH a AS (SELECT app_id, left(description, 2000) d FROM apps WHERE store=?
                   QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1),
             x AS (SELECT s.keyword, s.app_id, k.pats, k.rare,
                          lower(coalesce(s.name,'')||' '||coalesce(s.subtitle,'')||' '||coalesce(a.d,'')) txt
                   FROM sr_latest s JOIN _kp k USING (keyword) LEFT JOIN a USING (app_id)
                   WHERE s.store=? AND s.snapshot_date=?)
        SELECT keyword, app_id,
               CASE WHEN len(pats)=0 THEN 1.0
                    WHEN rare IS NOT NULL AND NOT regexp_matches(txt, rare) THEN 0.0
                    ELSE len(list_filter(pats, lambda p: regexp_matches(txt, p))) / len(pats) END
        FROM x""", [store, store, search_date]).fetchall()
    con.execute("DROP TABLE IF EXISTS _kp")
    return {(k, a): float(r) for k, a, r in rows}
