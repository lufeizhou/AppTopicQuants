"""How navigational a keyword is. Brand terms ('instagram') dominate search volume but
are not buildable topics. Heuristic from search results, blended with an LLM
probability when available (factor 'llm_brand')."""
from __future__ import annotations

import math
import re
import unicodedata

from ..registry import metrics
from .base import FactorRow, Metric, latest_snapshot

_tok = re.compile(r"[a-z0-9]+")


def norm(s: str) -> str:
    """lowercase, accents stripped (Pokémon -> pokemon)."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def tokens(s: str) -> set[str]:
    return set(_tok.findall(norm(s)))


def name_similarity(keyword: str, app_name: str) -> float:
    kw, an = tokens(keyword), tokens(app_name)
    if not kw or not an:
        return 0.0
    # Keyword fully contained in the name ('duolingo' in 'Duolingo: Learn Languages') is the brand signature;
    # so is the spaceless form ('prize picks' in 'PrizePicks', 'pokemon' in 'PokemonGO').
    if kw <= an or ("".join(sorted(kw)) and "".join(_tok.findall(norm(keyword))) in "".join(_tok.findall(norm(app_name)))):
        return 1.0
    return 0.5 * len(kw & an) / len(kw | an)


@metrics.register()
class Brandness(Metric):
    name = "brandness"
    description = "0..1: probability the term is a brand/navigational query rather than a generic need."
    requires = ("search_results",)
    higher_is_better = False

    def compute(self, con, store, snapshot_date):
        d = latest_snapshot(con, "search_results", "store", store, snapshot_date)
        if d is None:
            return []
        rows = con.execute("""
            SELECT keyword, rank, name, developer, rating_count FROM sr_latest
            WHERE store=? AND snapshot_date=? AND rank<=10 ORDER BY keyword, rank""", [store, d]).fetchall()
        llm = dict(con.execute("""
            SELECT entity_id, value FROM factor_values WHERE entity_type='keyword' AND metric='llm_brand'
            QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""").fetchall())
        by_kw: dict[str, list] = {}
        for r in rows:
            by_kw.setdefault(r[0], []).append(r)
        out = []
        for kw, lst in by_kw.items():
            top = lst[0]
            total = sum((r[4] or 0) for r in lst) or 1
            share = (top[4] or 0) / total
            sim = max(name_similarity(kw, top[2]), name_similarity(kw, top[3]))
            # keyword stuffing: when other results FROM OTHER DEVELOPERS also carry (half of) the keyword's
            # tokens, containment is generic ('hiit timer' in 'Interval Timer HIIT Timer' AND 'Timer Plus').
            # Same-developer families ('Amazon Shopping', 'Amazon Prime Video', 'Amazon Music') are a brand.
            kw_tokens, others = tokens(kw), lst[1:]
            top_dev = norm(top[3])
            family = sum(1 for r in lst if norm(r[3]) == top_dev)
            if others and kw_tokens:
                foreign = [r for r in others if norm(r[3]) != top_dev]
                need = math.ceil(len(kw_tokens) / 2)
                generic_rate = (sum(1 for r in foreign if len(kw_tokens & tokens(r[2])) >= need) / len(foreign)) if foreign else 0.0
                sim *= (1 - generic_rate)
            if family >= 3 and (name_similarity(kw, top[2]) == 1.0 or name_similarity(kw, top[3]) == 1.0):
                sim = max(sim, 1.0)   # a developer owning 3+ of the top-10 under the keyword's name is a brand
            # rating share only means "brand" when the top app is genuinely big (full weight at 1M ratings)
            scale = max(0.0, min(1.0, math.log10((top[4] or 0) + 1) / 6))
            heur = sim * (0.3 + 0.7 * share * scale)
            # navigational giant with a different name ('twitter' -> 'X'): one app with >= 1M ratings holding
            # >= 60% of the page's ratings. Generic terms owned by one giant also trip this, but those sectors are
            # excluded for concentration anyway; the DeepSeek classifier overrides when available.
            if (top[4] or 0) >= 1_000_000 and share >= 0.6:
                heur = max(heur, 0.3 + 0.7 * share)
            # the LLM classifier is the better judge of generic-vs-navigational; the heuristic is a fallback
            val = heur if kw not in llm else 0.3 * heur + 0.7 * llm[kw]
            out.append(FactorRow(kw, round(min(1.0, val), 4),
                                 {"top_app": top[2], "name_sim": round(sim, 3), "rating_share": round(share, 3),
                                  "llm": llm.get(kw)}))
        return out
