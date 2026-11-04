"""Your own apps as ground truth: their metadata terms, where they rank for them over time, and what the model
predicted for those terms (open spots -> page one). Pages for the own terms are refreshed by the daily light run,
so `search_results` holds a rank series per term."""
from __future__ import annotations

import datetime as dt
import re
import tomllib

from ..config import ROOT, settings

STOP = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "your", "you", "my", "&", "+", "by", "is", "it", "at", "new"}


def load_own_apps() -> list[dict]:
    p = ROOT / "config" / "own_apps.toml"
    if not p.exists():
        return []
    with p.open("rb") as f:
        return tomllib.load(f).get("app", [])


def own_terms(app: dict) -> list[dict]:
    """Single words and 2-word phrases from title and subtitle (app name excluded) plus the keyword field."""
    name_words = set(re.findall(r"[a-z0-9]+", (app.get("name") or "").lower()))
    terms, seen = [], set()

    def add(t, field):
        t = t.strip().lower()
        if t and t not in seen and t not in STOP:
            seen.add(t)
            terms.append({"term": t, "field": field})
    for field in ("title", "subtitle"):
        words = [w for w in re.findall(r"[a-z0-9]+", (app.get(field) or "").lower()) if w not in STOP and w not in name_words]
        for w in words:
            add(w, field)
        for i in range(len(words) - 1):
            add(f"{words[i]} {words[i + 1]}", field)
    for t in (app.get("keywords") or "").split(","):
        add(t, "keywords")
    for t in app.get("watch") or []:            # extra searches you want tracked for this app
        add(t, "watch")
    return terms


def all_own_terms() -> list[str]:
    out: dict[str, None] = {}
    for a in load_own_apps():
        for t in own_terms(a):
            out.setdefault(t["term"])
    return list(out)


def refresh_own_pages(store: str = settings.store) -> int:
    """Fetch today's results page for every own term (iTunes, free; ~1 request per term). Lock-friendly."""
    from .discover import search_terms
    terms = all_own_terms()
    return search_terms(terms, store) if terms else 0


def rank_history(con, store: str, app_id: str, terms: list[str]) -> dict[str, list[list]]:
    """term -> [[date, rank or None], ...] over every day the term's page was fetched (None = not in the top 200)."""
    if not terms:
        return {}
    fetched = con.execute("""SELECT DISTINCT keyword, snapshot_date FROM search_results WHERE store=? AND keyword IN (SELECT unnest(?))
                             ORDER BY keyword, snapshot_date""", [store, terms]).fetchall()
    ranks = {(k, d): r for k, d, r in con.execute("""SELECT keyword, snapshot_date, min(rank) FROM search_results
                                                     WHERE store=? AND app_id=? AND keyword IN (SELECT unnest(?)) GROUP BY 1, 2""",
                                                  [store, app_id, terms]).fetchall()}
    out: dict[str, list[list]] = {}
    for k, d in fetched:
        out.setdefault(k, []).append([d.isoformat() if isinstance(d, dt.date) else str(d), ranks.get((k, d))])
    return out
