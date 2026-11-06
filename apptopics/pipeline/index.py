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
             Grades are evidence checks (earn_grade, 2026-11-05): A can earn (band >= $5k, 2+ young winners,
             popularity >= 30, buildable) -> Build now; B maybe -> Maybe; ? not sure -> Not sure; C no -> No;
             "-" No data. The score orders
             searches inside a grade.
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


JUDGED = ("buildability",)   # per-keyword LLM judgements, written incrementally


def keyword_factors(con, metrics: tuple[str, ...], dc: dt.date) -> tuple[dict[str, dict], dict[str, dict]]:
    """Latest value (and meta for attainable / intent) per keyword and metric, with the same freshness rule as the
    composite: a metric recomputed on the composite date dc only counts from that date on, so a keyword whose sector
    shrank below the minimum does not keep an old band; metrics not recomputed that day keep their latest row.
    JUDGED metrics (LLM verdicts on a few keywords at a time) always keep each keyword's latest row: judging new
    keywords today must not void older judgements."""
    F: dict[str, dict] = {}
    M: dict[str, dict] = {}
    for e, m, v, mt in con.execute("""WITH fresh AS (SELECT DISTINCT metric FROM factor_values WHERE entity_type='keyword' AND snapshot_date=?)
            SELECT entity_id, metric, value, meta FROM factor_values WHERE entity_type='keyword' AND metric IN (SELECT unnest(?))
              AND (snapshot_date >= ? OR metric NOT IN (SELECT metric FROM fresh) OR metric IN (SELECT unnest(?)))
            QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1""", [dc, list(metrics), dc, list(JUDGED)]).fetchall():
        F.setdefault(e, {})[m] = v
        if m in ("attainable", "intent") and mt:
            M.setdefault(e, {})[m] = json.loads(mt)
    return F, M


def searches_per_day(pop: float | None) -> float:
    u = settings.section("universe")
    return 0.0 if not pop else float(u.get("searches_per_day_at_40", 70.0)) * float(u.get("searches_per_day_per_point", 1.15)) ** (pop - 40)


def popularity_for(spd: float | None) -> float | None:
    """Inverse of searches_per_day: the popularity that gives `spd` searches a day (for aspects that sum several searches)."""
    u = settings.section("universe")
    if not spd or spd <= 0:
        return None
    return 40 + math.log(spd / float(u.get("searches_per_day_at_40", 70.0))) / math.log(float(u.get("searches_per_day_per_point", 1.15)))


def _grade(score: float) -> str:
    # absolute cut-offs on the multiplicative score; real opportunities are rare (A ~ top 2% of visible niches)
    g = settings.section("grades")
    return ("A" if score >= g.get("A", 50) else "B" if score >= g.get("B", 38) else "C" if score >= g.get("C", 28)
            else "D" if score >= g.get("D", 18) else "E")


ACTIONS = {"A": "Build now", "B": "Maybe", "?": "Not sure", "C": "No"}
GRADE_ORDER = {"A": 0, "B": 1, "?": 2, "C": 3, "-": 4}


def earn_grade(money_usd: float | None, young: float | None, popularity: float | None, build: float | None,
               unknown: str | None = None) -> tuple[str, dict, str]:
    """Three grades from evidence, not score cut-offs (decided 2026-11-05, [step6.grades]):
    A  can earn: p5-p20 apps earn >= a_money_usd AND >= a_young_min apps under 2 years earn the target AND
       popularity >= a_min_popularity AND judged buildable (>= a_min_buildability)
    B  maybe: money exists (band >= b_money_usd or a young winner) but a check fails (or buildability not judged)
    ?  not sure: the market cannot be measured (sector too thin, popularity unknown, nobody serves it yet)
    C  no: no money signal, or judged hard to build for us (brand, rubbish, games: C before this is called)
    Returns (grade, checks, reason). The opportunity score only orders searches inside a grade."""
    g = settings.section("grades")
    a_money, a_young, a_pop = float(g.get("a_money_usd", 5000)), float(g.get("a_young_min", 2)), float(g.get("a_min_popularity", 30))
    a_build, b_money = float(g.get("a_min_buildability", 0.6)), float(g.get("b_money_usd", 1000))
    m, y = money_usd or 0.0, young or 0.0
    checks = {"money": m >= a_money, "newcomers": y >= a_young, "traffic": popularity is not None and popularity >= a_pop,
              "build": None if build is None else build >= a_build}
    facts = {"money": f"p5-p20 apps earn ~${m:,.0f}/mo", "newcomers": f"{y:.0f} young app{'s earn' if round(y) != 1 else ' earns'} $5k+",
             "traffic": f"popularity {popularity:.0f} (~{searches_per_day(popularity):,.0f}/day)" if popularity is not None else "popularity unknown",
             "build": "buildability not judged yet" if build is None else f"buildable {build:.2f}"}
    line = lambda ks: " · ".join(facts[k] for k in ks)
    if unknown:
        return "?", checks, f"not sure: {unknown}"
    if build is not None and not checks["build"]:
        return "C", checks, f"hard to build for us ({build:.2f}) · " + line(["money", "newcomers"])
    if all(checks.values()):
        return "A", checks, line(["newcomers", "money", "traffic", "build"])
    if m < b_money and y < 1:
        return "C", checks, f"no money: {line(['money', 'newcomers'])}"
    failed = [k for k, v in checks.items() if not v]
    return "B", checks, "missing: " + line(failed) + "  |  has: " + line([k for k, v in checks.items() if v])


def specificity_mult(specificity: int | None) -> float:
    """Score multiplier from the label's specificity (1-5): the specificity_mults table if set (anchored: only vague
    searches are cut), else base + step x (s - 1); unknown -> specificity_unknown."""
    lb = settings.section("labels")
    if specificity is None:
        return float(lb.get("specificity_unknown", 1.0))
    s = max(1, min(5, int(specificity)))
    if lb.get("specificity_mults"):
        return float(lb["specificity_mults"][s - 1])
    return min(float(lb.get("specificity_cap", 1.0)), float(lb.get("specificity_base", 0.5)) + float(lb.get("specificity_step", 0.125)) * (s - 1))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def demand_part(pop: float | None) -> float:
    spd = searches_per_day(pop)
    full = float(settings.section("score").get("demand_full_per_day", 2000))
    return _clip(math.log10(spd + 1) / math.log10(full + 1)) if spd else 0.0


def opportunity_parts(f: dict, meta: dict) -> dict:
    usd = (meta.get("attainable") or {}).get("attainable_usd")
    p5k = f.get("breadth")
    if usd is None or p5k is None:
        return {}
    w = settings.section("score")
    lo, hi = math.log10(float(w.get("band_usd_zero", 1000))), math.log10(float(w.get("band_usd_full", 30000)))
    bs = float(w.get("money_band_share", 0.5))
    money = bs * _clip((math.log10(max(usd, 1)) - lo) / (hi - lo)) + (1 - bs) * _clip(p5k / float(w.get("p5k_full", 0.4)))
    nw = f.get("newcomer_winners")   # young apps already earning the target anywhere in the sector (newcomers_full of them -> full credit)
    newcomers = math.expm1(nw) if nw is not None else None
    if newcomers is not None:
        sh = float(w.get("newcomers_share", 0.3))
        money = (1 - sh) * money + sh * _clip(newcomers / float(w.get("newcomers_full", 4)))
    ad = f.get("ad_activity")        # competitors keep paying for Meta ads: users are worth more than the ads
    if ad is not None:
        sh = float(w.get("ads_share", 0.15))
        money = (1 - sh) * money + sh * ad
    cpc = f.get("cpc")
    if cpc is not None:   # DataForSEO: advertisers pay where buyers are (cpc_zero -> 0, cpc_full -> 1)
        sh, cz = float(w.get("cpc_share", 0.2)), float(w.get("cpc_zero", 0.2))
        money = (1 - sh) * money + sh * _clip((cpc - cz) / (float(w.get("cpc_full", 3.0)) - cz))
    dif = f.get("difficulty")
    rc = settings.section("reach")
    mid, slope = float(rc.get("midpoint", 70)), float(rc.get("slope", 8))
    curve = 1.0 / (1.0 + math.exp(((dif if dif is not None else mid) - mid) / slope))
    reach = _clip(float(w.get("reach_curve_share", 0.5)) * curve
                  + float(w.get("reach_weak_share", 0.3)) * min(1.0, (f.get("weak_spots") or 0) / float(w.get("reach_weak_full", 4)))
                  + float(w.get("reach_entrant_share", 0.2)) * (f.get("entrant_share") or 0))
    demand = demand_part(f.get("demand"))
    sv = f.get("search_volume")   # log10 Google monthly searches (DataForSEO): google_log_zero -> 0, google_log_full -> 1
    so = f.get("social_views")    # log10 TikTok + YouTube views: social_log_zero -> 0, social_log_full -> 1
    gz, gf = float(w.get("google_log_zero", 2)), float(w.get("google_log_full", 5))
    sz, sf = float(w.get("social_log_zero", 4)), float(w.get("social_log_full", 7))
    g_part = _clip((sv - gz) / (gf - gz)) if sv is not None else None
    s_part = _clip((so - sz) / (sf - sz)) if so is not None else None
    if demand and g_part is not None:
        sh = float(w.get("google_share", 0.25))
        demand = (1 - sh) * demand + sh * g_part
    if demand and s_part is not None:                 # off-store interest can only add: few videos does not mean few buyers
        sh = float(w.get("social_share", 0.25))
        demand = max(demand, (1 - sh) * demand + sh * s_part)
    conc = f.get("concentration")
    cf, cs = float(w.get("conc_free", 0.5)), float(w.get("conc_span", 0.45))
    c_adj = 1 - float(w.get("conc_max_penalty", 0.5)) * _clip(((conc or cf) - cf) / cs)
    b = f.get("buildability")
    bb = float(w.get("build_base", 0.5))
    b_adj = bb + (1 - bb) * b if b is not None else float(w.get("build_unknown", 0.85))
    tr = f.get("trending")
    tm = float(w.get("trend_max", 0.15))
    t_adj = 1 + _clip((tr or 0) / float(w.get("trend_apple_div", 20)), -tm, tm)
    g = f.get("trend_slope")      # Google 12-month growth vs prior years (DataForSEO Trends), e.g. +0.3 = +30%
    if g is not None:
        t_adj = 0.5 * t_adj + 0.5 * (1 + _clip(g / float(w.get("trend_google_div", 2)), -tm, tm))
    score = (100 * (money ** float(w.get("money_exp", 0.45))) * (reach ** float(w.get("reach_exp", 0.35)))
             * (demand ** float(w.get("demand_exp", 0.20))) * c_adj * b_adj * t_adj)
    return {"score": round(_clip(score, 0, 100), 1), "money": round(money, 2), "reach": round(reach, 2), "demand": round(demand, 2),
            "conc_adj": round(c_adj, 2), "build_adj": round(b_adj, 2), "trend_adj": round(t_adj, 2),
            "uses_google": cpc is not None or sv is not None or g is not None,
            "uses_social": so is not None or ad is not None,
            # inputs, so a report can show how the score was built
            "in": {"band_usd": usd, "p5k": round(p5k, 3), "newcomers": None if newcomers is None else round(newcomers, 1), "ad_activity": ad,
                   "cpc": cpc, "difficulty": dif, "weak_spots": f.get("weak_spots"), "entrant_share": f.get("entrant_share"),
                   "popularity": f.get("demand"), "google_volume": None if sv is None else round(10 ** sv), "video_views": None if so is None else round(10 ** so),
                   "concentration": conc, "buildability": b, "trending": tr, "google_growth": g}}


HARD_HIDE = ("label:", "non-latin", "excluded term", "games", "buildability<", "thin sector", "coverage", "other language")


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
        # thin sector / low coverage are "hard" for the composite but mean "unknown", not "bad", for the index
        hard_bad = hidden and any(h in why_hidden for h in HARD_HIDE if h not in ("thin sector", "coverage", "other language"))
        judged = m.get("label") not in ("brand", "rubbish") and not hard_bad
        unproven = judged and not parts                                   # no sector to measure
        nopage = unproven and f.get("difficulty") is None and f.get("intent") is None   # results page never fetched
        nopop = judged and bool(parts) and f.get("demand") is None        # sector measured, search popularity never fetched
        score = 0.0 if (m.get("label") in ("brand", "rubbish") or not parts or nopop) else round(parts["score"] * specificity_mult(m.get("specificity")), 1)
        b = f.get("buildability")
        money = (meta.get("attainable") or {}).get("attainable_usd")
        young = (meta.get("newcomer_winners") or {}).get("count")
        if young is None and f.get("newcomer_winners") is not None:
            young = math.expm1(f["newcomer_winners"])
        checks: dict = {}
        if nopage:
            grade, action = "-", "No data"   # nothing fetched yet: not a market judgement at all
            reason = "results page not fetched yet" + (f" · popularity {f['demand']:.0f}" if f.get("demand") is not None else "")
        elif m.get("label") in ("brand", "rubbish") or hard_bad:
            grade, reason = "C", ("brand or app name: not an open need" if m.get("label") == "brand" else
                                  "not a real search" if m.get("label") == "rubbish" else f"outside what we build: {why_hidden}")
        elif unproven:
            n_rel = (meta.get("intent") or {}).get("relevant_apps")
            grade, checks, reason = earn_grade(money, young, f.get("demand"), b, unknown="too few apps serve this search to judge the market"
                                               + (f" ({n_rel} relevant apps; {min_n} needed)" if n_rel is not None else ""))
        elif nopop:
            grade, checks, reason = earn_grade(money, young, None, b, unknown="search popularity not measured yet")
        else:
            grade, checks, reason = earn_grade(money, young, f.get("demand"), b)
            if hidden and why_hidden:
                reason += f"  |  note: {why_hidden}"   # soft reasons (leader share, small band) are shown, not a gate
            if (meta.get("attainable") or {}).get("read_as"):   # typo / other language: competitors found by Apple's word
                reason += f"  |  note: read as '{meta['attainable']['read_as']}' (Apple's top results)"
        if grade != "-":
            action = ACTIONS[grade]
        out.append({"entity_type": "keyword", "entity_id": k, "metric": "opp_score", "value": score,
                    "meta": json.dumps({"grade": grade, "action": action, "reason": reason, "checks": checks, "parts": parts,
                                        "unknown": bool(unproven or nopop) and not nopage}),
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
            a = settings.section("aso")
            ef = float(a.get("ease_floor", 0.1))
            ease = ef + (1 - ef) * min(1.0, (ws or 0) / float(a.get("weak_full", 5)))
            score = round(100 * (demand_part(pop) ** float(a.get("demand_exp", 0.5))) * (ease ** float(a.get("ease_exp", 0.5))), 1)
            tp = float(a.get("target_min_popularity", 20))
            action = ("Skip" if pop < float(a.get("skip_below_popularity", 15)) else "Target now" if (ws or 0) >= float(a.get("target_min_weak", 3)) and pop >= tp
                      else "Later (needs ratings)" if pop >= tp else "Skip")
            reason = f"~{searches_per_day(pop):,.0f} searches/day · {ws if ws is not None else '?'} weak spots in top 10"
        out.append({"entity_type": "keyword", "entity_id": k, "metric": "aso_score", "value": score,
                    "meta": json.dumps({"grade": _grade(score), "action": action, "reason": reason}), "snapshot_date": d, "computed_at": now})
    con.execute("DELETE FROM factor_values WHERE metric IN ('opp_score','aso_score') AND snapshot_date=?", [d])
    upsert(con, "factor_values", out)
    return {"opp": len(comp), "aso": n_aso}
