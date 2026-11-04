"""Recommendation index: turn factors into a suggestion, like an analyst rating.

Two scores per keyword (0-100), each with a grade (A-E), an action and a one-line reason:

  opp_score  New-app opportunity: should we BUILD an app for this need? ABSOLUTE, not a percentile:
             100 x money^0.45 x reach^0.35 x demand^0.20 x concentration x buildability x trend
               money  = 0.5 x band revenue on a log scale ($1k -> 0, $30k -> 1) + 0.5 x P(app >= $5k)/0.4
                        then 70/30 with young winners (apps < 2 years old earning the target anywhere in the sector, 4 -> 1),
                        then 85/15 with Meta ad activity and 80/20 with Google CPC, when known
               reach  = 0.5 x difficulty curve (0.5 at 70) + 0.3 x weak spots/4 + 0.2 x newcomer share
               demand = log searches/day (1/day -> 0, 2,000/day -> 1), blended 75/25 with Google volume when known;
                        TikTok + YouTube views (10k -> 0, 10M -> 1) can then only raise it (75/25 blend, never lower)
             A keyword whose sector is too thin to judge (< 30 relevant apps) gets grade ? and action Unproven, not E.
             A multiplicative score: a fatal weakness in one part cannot be bought back by the others.
             Grades: A >= 50, B >= 38, C >= 28, D >= 18, else E. Hidden niches keep their score, action Avoid.
             Build now : A/B and not hidden    Watch : C (or A/B hidden only for a soft reason)    Avoid : rest
  aso_score  ASO keyword: should an EXISTING app in this space TARGET it in title/subtitle/keywords?
             100 x demand^0.5 x ease^0.5, ease = 0.1 + 0.9 x min(1, weak spots/5); same grade cut-offs.
             Target now : >= 3 weak spots and popularity >= 20
             Later      : popular but 0-2 weak spots (needs ratings first)
             Skip       : brand/rubbish label, or popularity < 15
"""
from __future__ import annotations

import datetime as dt
import json
import math

from ..config import settings
from ..db import today, upsert

INDEX_METRICS = ('attainable', 'breadth', 'concentration', 'difficulty', 'weak_spots', 'demand', 'trending', 'entrant_share',
                 'indie_share', 'intent', 'buildability', 'competition', 'monetization', 'cpc', 'search_volume', 'trend_slope',
                 'newcomer_winners', 'social_views', 'ad_activity')


def composite_date(con) -> dt.date | None:
    return con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='composite' AND entity_type='keyword'").fetchone()[0]


def keyword_factors(con, metrics: tuple[str, ...], dc: dt.date) -> tuple[dict[str, dict], dict[str, dict]]:
    """Latest value (and meta for attainable / intent) per keyword and metric, with the same freshness rule as the
    composite: a metric recomputed on the composite date dc only counts from that date on, so a keyword whose sector
    shrank below the minimum does not keep an old band; metrics not recomputed that day keep their latest row."""
    F: dict[str, dict] = {}
    M: dict[str, dict] = {}
    for e, m, v, mt in con.execute("""WITH fresh AS (SELECT DISTINCT metric FROM factor_values WHERE entity_type='keyword' AND snapshot_date=?)
            SELECT entity_id, metric, value, meta FROM factor_values WHERE entity_type='keyword' AND metric IN (SELECT unnest(?))
              AND (snapshot_date >= ? OR metric NOT IN (SELECT metric FROM fresh))
            QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1""", [dc, list(metrics), dc]).fetchall():
        F.setdefault(e, {})[m] = v
        if m in ("attainable", "intent") and mt:
            M.setdefault(e, {})[m] = json.loads(mt)
    return F, M


def searches_per_day(pop: float | None) -> float:
    return 0.0 if not pop else 70.0 * 1.15 ** (pop - 40)


def _grade(score: float) -> str:
    # absolute cut-offs on the multiplicative score; real opportunities are rare (A ~ top 2% of visible niches)
    return "A" if score >= 50 else "B" if score >= 38 else "C" if score >= 28 else "D" if score >= 18 else "E"


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def demand_part(pop: float | None) -> float:
    spd = searches_per_day(pop)
    return _clip(math.log10(spd + 1) / math.log10(2001)) if spd else 0.0


def opportunity_parts(f: dict, meta: dict) -> dict:
    usd = (meta.get("attainable") or {}).get("attainable_usd")
    p5k = f.get("breadth")
    if usd is None or p5k is None:
        return {}
    money = 0.5 * _clip((math.log10(max(usd, 1)) - 3) / (math.log10(30000) - 3)) + 0.5 * _clip(p5k / 0.4)
    nw = f.get("newcomer_winners")   # young apps already earning the target anywhere in the sector (4 of them -> full credit)
    newcomers = math.expm1(nw) if nw is not None else None
    if newcomers is not None:
        money = 0.7 * money + 0.3 * _clip(newcomers / 4)
    ad = f.get("ad_activity")        # competitors keep paying for Meta ads: users are worth more than the ads
    if ad is not None:
        money = 0.85 * money + 0.15 * ad
    cpc = f.get("cpc")
    if cpc is not None:   # DataForSEO: advertisers pay where buyers are ($0.20 -> 0, $3 -> 1)
        money = 0.8 * money + 0.2 * _clip((cpc - 0.2) / 2.8)
    dif = f.get("difficulty")
    rc = settings.weights().get("reach", {})
    mid, slope = float(rc.get("midpoint", 70)), float(rc.get("slope", 8))
    curve = 1.0 / (1.0 + math.exp(((dif if dif is not None else mid) - mid) / slope))
    reach = _clip(0.5 * curve + 0.3 * min(1.0, (f.get("weak_spots") or 0) / 4) + 0.2 * (f.get("entrant_share") or 0))
    demand = demand_part(f.get("demand"))
    sv = f.get("search_volume")   # log10 Google monthly searches (DataForSEO): 2 -> 0, 5 -> 1
    so = f.get("social_views")    # log10 TikTok + YouTube views: 10k -> 0, 10M -> 1
    g_part = _clip((sv - 2) / 3) if sv is not None else None
    s_part = _clip((so - 4) / 3) if so is not None else None
    if demand and g_part is not None:
        demand = 0.75 * demand + 0.25 * g_part
    if demand and s_part is not None:                 # off-store interest can only add: few videos does not mean few buyers
        demand = max(demand, 0.75 * demand + 0.25 * s_part)
    conc = f.get("concentration")
    c_adj = 1 - 0.5 * _clip(((conc or 0.5) - 0.5) / 0.45)
    b = f.get("buildability")
    b_adj = 0.5 + 0.5 * b if b is not None else 0.85
    tr = f.get("trending")
    t_adj = 1 + _clip((tr or 0) / 20, -0.15, 0.15)
    g = f.get("trend_slope")      # Google 12-month growth vs prior years (DataForSEO Trends), e.g. +0.3 = +30%
    if g is not None:
        t_adj = 0.5 * t_adj + 0.5 * (1 + _clip(g / 2, -0.15, 0.15))
    score = 100 * (money ** 0.45) * (reach ** 0.35) * (demand ** 0.20) * c_adj * b_adj * t_adj
    return {"score": round(_clip(score, 0, 100), 1), "money": round(money, 2), "reach": round(reach, 2), "demand": round(demand, 2),
            "conc_adj": round(c_adj, 2), "build_adj": round(b_adj, 2), "trend_adj": round(t_adj, 2),
            "uses_google": cpc is not None or sv is not None or g is not None,
            "uses_social": so is not None or ad is not None,
            # inputs, so a report can show how the score was built
            "in": {"band_usd": usd, "p5k": round(p5k, 3), "newcomers": None if newcomers is None else round(newcomers, 1), "ad_activity": ad,
                   "cpc": cpc, "difficulty": dif, "weak_spots": f.get("weak_spots"), "entrant_share": f.get("entrant_share"),
                   "popularity": f.get("demand"), "google_volume": None if sv is None else round(10 ** sv), "video_views": None if so is None else round(10 ** so),
                   "concentration": conc, "buildability": b, "trending": tr, "google_growth": g}}


HARD_HIDE = ("label:", "non-latin", "excluded term", "games", "buildability<", "thin sector", "coverage")


def _phrase(m: str, f: dict, meta: dict) -> str | None:
    v = f.get(m)
    if v is None:
        return None
    usd = (meta.get("attainable") or {}).get("attainable_usd")
    return {
        "attainable": f"p5-p20 apps earn ~${usd:,.0f}/mo" if usd is not None else None,
        "breadth": f"P(app >= $5k) {v:.0%}",
        "concentration": "money spread out" if v < 0.55 else "top-heavy market",
        "difficulty": f"easy to enter ({v:.0f})" if v < 55 else f"hard to enter ({v:.0f})",
        "weak_spots": f"{v:.0f} weak spots in top 10",
        "demand": f"popularity {v:.0f}",
        "trending": f"rising {v:+.0f}" if v > 0 else f"falling {v:+.0f}",
        "entrant_share": "newcomers reach the band" if v >= 0.3 else "few newcomers succeed",
        "indie_share": "indie-friendly" if v >= 0.6 else "big developers dominate",
        "intent": "clear search intent" if v >= 0.8 else "mixed search intent",
        "buildability": "fits our constraints" if v >= 0.6 else "hard to build for us",
        "competition": "weak incumbents" if v < 3 else "strong incumbents",
        "monetization": "market pays overall",
        "cpc": f"Google CPC ${v:.2f}",
        "search_volume": f"~{10 ** v:,.0f} Google searches/mo",
        "trend_slope": ("Google interest up >3x vs prior years (from a small base)" if v > 2 else f"Google interest {v:+.0%} vs prior years"),
        "newcomer_winners": f"~{math.expm1(v):.0f} young apps already earn $5k+" if math.expm1(v) >= 0.5 else "no young app earns $5k+ yet",
        "social_views": f"~{10 ** v / 1e6:.1f}M video views" if v >= 6 else f"~{10 ** v / 1e3:.0f}k video views",
        "ad_activity": "competitors keep paying for ads" if v >= 0.3 else "little paid acquisition",
    }.get(m)


def build_index(con, store: str, snapshot_date: dt.date | None = None) -> dict:
    d = snapshot_date or today()
    now = dt.datetime.now()
    dc = d
    rows = con.execute("SELECT entity_id, value, meta FROM factor_values WHERE metric='composite' AND entity_type='keyword' AND snapshot_date=?",
                       [d]).fetchall()
    if not rows:                      # rescoring on a day without a full scoring run: use the latest composite
        dc = composite_date(con)
        if dc is None:
            return {"opp": 0, "aso": 0}
        rows = con.execute("SELECT entity_id, value, meta FROM factor_values WHERE metric='composite' AND entity_type='keyword' AND snapshot_date=?",
                           [dc]).fetchall()
    comp = {k: (v, json.loads(m) if m else {}) for k, v, m in rows}
    F, M = keyword_factors(con, INDEX_METRICS, dc)
    min_n = int(settings.weights().get("target", {}).get("min_sector_size", 30))
    out = []
    for k, (_v, m) in comp.items():
        f, meta = F.get(k, {}), M.get(k, {})
        parts = opportunity_parts(f, meta)
        hidden = bool(m.get("excluded"))
        why_hidden = m.get("excluded_reason") or ""
        hard = hidden and any(h in why_hidden for h in HARD_HIDE)
        # thin sector / low coverage are "hard" for the composite but mean "unknown", not "bad", for the index
        hard_bad = hidden and any(h in why_hidden for h in HARD_HIDE if h not in ("thin sector", "coverage"))
        judged = m.get("label") not in ("brand", "rubbish") and not hard_bad
        unproven = judged and not parts                                   # no sector to measure
        nopage = unproven and f.get("difficulty") is None and f.get("intent") is None   # results page never fetched
        nopop = judged and bool(parts) and f.get("demand") is None        # sector measured, search popularity never fetched
        if m.get("label") in ("brand", "rubbish") or not parts or nopop:  # hidden niches with a sector keep their grade (action Avoid)
            score, grade = 0.0, "?" if (unproven or nopop) else "E"
        else:
            score = parts["score"] * (0.7 if m.get("label") == "broad" else 0.5 if m.get("label") == "vague" else 1.0)
            score = round(score, 1)
            grade = _grade(score)
        b = f.get("buildability")
        buildable = b is not None and b >= 0.6
        if nopage:
            action = "No data"        # nothing fetched yet: not a market judgement at all
        elif unproven or nopop:
            action = "Unproven"       # not bad, unknown: no sector to judge, or no popularity figure yet
        elif hard or (hidden and grade not in "ABC"):
            action = "Avoid"
        elif hidden:
            action = "Watch"            # good numbers, hidden only for a soft reason (e.g. concentration, small band)
        else:
            action = "Build now" if grade in "AB" and buildable else "Watch" if grade in "ABC" else "Avoid"
        contrib = m.get("contrib") or {}
        pos = [x for x, c in sorted(contrib.items(), key=lambda kv: -kv[1]) if c > 0.15][:3]
        neg = [x for x, c in sorted(contrib.items(), key=lambda kv: kv[1]) if c < -0.15][:2]
        pros = [p for p in (_phrase(x, f, meta) for x in pos) if p]
        cons = [p for p in (_phrase(x, f, meta) for x in neg) if p]
        reason = " · ".join(pros) + (("  |  but " + ", ".join(cons)) if cons else "")
        if grade in "AB" and action == "Watch" and not hidden and not buildable:
            reason += "  |  Watch until buildability is judged" if b is None else f"  |  Watch: hard to build for us ({b:.1f})"
        if nopage:
            reason = "results page not fetched yet" + (f" · popularity {f['demand']:.0f}" if f.get("demand") is not None else "")
        elif unproven:
            n_rel = (meta.get("intent") or {}).get("relevant_apps")
            reason = ("too few apps serve this search to judge the market" + (f" ({n_rel} relevant apps; {min_n} needed)" if n_rel is not None else "")
                      + "; look at the aspect verdict and social signals instead")
        elif nopop:
            reason = "search popularity unknown (not in Apple's weekly list and not measured by Astro yet); the market numbers are stored but not graded"
        elif hidden:
            reason = f"hidden: {why_hidden}" + (f"  ·  {reason}" if reason and not hard else "")
        out.append({"entity_type": "keyword", "entity_id": k, "metric": "opp_score", "value": score,
                    "meta": json.dumps({"grade": grade, "action": action, "reason": reason, "parts": parts}),
                    "snapshot_date": d, "computed_at": now})
    # ASO keyword score (absolute)
    n_aso = 0
    for k, (_v, m) in comp.items():
        f = F.get(k, {})
        pop, ws = f.get("demand"), f.get("weak_spots")
        if m.get("label") in ("brand", "rubbish") or pop is None:
            score, action, reason = 0.0, "Skip", "brand or no popularity data"
        else:
            n_aso += 1
            ease = 0.1 + 0.9 * min(1.0, (ws or 0) / 5)
            score = round(100 * (demand_part(pop) ** 0.5) * (ease ** 0.5), 1)
            action = ("Skip" if pop < 15 else "Target now" if (ws or 0) >= 3 and pop >= 20
                      else "Later (needs ratings)" if pop >= 20 else "Skip")
            reason = f"~{searches_per_day(pop):,.0f} searches/day · {ws if ws is not None else '?'} weak spots in top 10"
        out.append({"entity_type": "keyword", "entity_id": k, "metric": "aso_score", "value": score,
                    "meta": json.dumps({"grade": _grade(score), "action": action, "reason": reason}), "snapshot_date": d, "computed_at": now})
    con.execute("DELETE FROM factor_values WHERE metric IN ('opp_score','aso_score') AND snapshot_date=?", [d])
    upsert(con, "factor_values", out)
    return {"opp": len(comp), "aso": n_aso}
