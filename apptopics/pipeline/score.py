"""Run every registered metric, store factor values, then compute the composite."""
from __future__ import annotations

import datetime as dt
import json
import logging
import math

import numpy as np

from ..config import settings
from ..db import today, upsert
from ..metrics import metrics
from ..metrics.base import has_rows

log = logging.getLogger("apptopics.score")


def run_metrics(con, store: str = settings.store, snapshot_date: dt.date | None = None,
                only: list[str] | None = None) -> dict[str, int]:
    d, now, counts = snapshot_date or today(), dt.datetime.now(), {}
    from ..metrics import sector
    from ..metrics.base import ensure_sr_latest
    sector._cache.clear()  # per-run cache of per-app revenue estimates
    ensure_sr_latest(con, d)
    for name, cls in metrics.items():
        if only and name not in only:
            continue
        m = cls()
        missing = [t for t in m.requires if not has_rows(con, t)]
        if missing:
            log.info("skip %s: no data in %s", name, missing)
            counts[name] = 0
            continue
        try:
            rows = m.compute(con, store, d)
        except Exception:
            log.exception("metric %s failed", name)
            counts[name] = 0
            continue
        con.execute("DELETE FROM factor_values WHERE metric=? AND snapshot_date=?", [name, d])
        counts[name] = upsert(con, "factor_values", [
            {"entity_type": r.entity_type or m.entity_type, "entity_id": r.entity_id, "metric": name, "value": r.value,
             "meta": json.dumps(r.meta) if r.meta else None, "snapshot_date": d, "computed_at": now} for r in rows])
        log.info("%s: %d", name, counts[name])
    return counts


def factor_matrix(con, store: str, snapshot_date: dt.date, entity_type: str = "keyword"):
    """Latest value per (entity, metric) on or before snapshot_date -> {entity: {metric: value}}."""
    # A metric computed in this run (has rows dated snapshot_date) uses ONLY this run's values: an entity it no
    # longer covers must not inherit a stale value from an older model version. Metrics not recomputed in this
    # run (LLM judgments, slow external pulls) fall back to their latest value.
    from .index import JUDGED  # judged a few keywords at a time: always keep each one's latest
    rows = con.execute("""
        WITH fresh AS (SELECT DISTINCT metric FROM factor_values WHERE entity_type=? AND snapshot_date=?)
        SELECT entity_id, metric, value FROM factor_values
        WHERE entity_type=? AND snapshot_date<=? AND metric NOT IN ('composite','opp_score','aso_score')
          AND (snapshot_date=? OR metric NOT IN (SELECT metric FROM fresh) OR metric IN (SELECT unnest(?)))
        QUALIFY row_number() OVER (PARTITION BY entity_id, metric ORDER BY snapshot_date DESC)=1""",
        [entity_type, snapshot_date, entity_type, snapshot_date, snapshot_date, list(JUDGED)]).fetchall()
    out: dict[str, dict[str, float]] = {}
    for e, m, v in rows:
        out.setdefault(e, {})[m] = v
    return out


def zscores(values: dict[str, float]) -> dict[str, float]:
    if len(values) < 2:
        return {k: 0.0 for k in values}
    arr = np.array(list(values.values()), dtype=float)
    mu, sd = float(np.nanmean(arr)), float(np.nanstd(arr))
    if sd == 0 or math.isnan(sd):
        return {k: 0.0 for k in values}
    return {k: float(np.clip((v - mu) / sd, -3, 3)) for k, v in values.items()}


ROLLUP_MAX = {"demand", "trend_level", "search_volume", "play_demand", "monetization"}


def composite(con, store: str = settings.store, snapshot_date: dt.date | None = None, entity_type: str = "keyword") -> int:
    d, now = snapshot_date or today(), dt.datetime.now()
    cfg = settings.weights()
    weights: dict[str, float] = cfg.get("composite", {})
    mat = factor_matrix(con, store, d, entity_type)
    if not mat:
        return 0
    # z-score each metric across the universe of entities that have it
    z: dict[str, dict[str, float]] = {}
    for m in weights:
        vals = {e: f[m] for e, f in mat.items() if m in f and f[m] is not None}
        z[m] = zscores(vals)
    filters = cfg.get("filters", {})
    max_conc = filters.get("max_concentration")
    conc_pct = filters.get("max_concentration_pct")
    if conc_pct is not None:
        cv = sorted(f["concentration"] for f in mat.values() if f.get("concentration") is not None)
        max_conc = round(cv[int(conc_pct * (len(cv) - 1))], 3) if cv else None
    labels: dict[str, str] = {}
    spec: dict[str, int | None] = {}
    if entity_type == "keyword" and con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='keyword_labels'").fetchone()[0]:
        for k, lab, sp in con.execute("SELECT keyword, label, specificity FROM keyword_labels WHERE store=?", [store]).fetchall():
            labels[k], spec[k] = lab, sp
    hide = set(filters.get("hide_labels", ["brand", "rubbish"]))
    spec_pen = float(cfg.get("labels", {}).get("composite_penalty", 8.0))
    require_sector = filters.get("require_sector", True)
    min_build = filters.get("min_buildability")
    min_band = filters.get("min_band_usd")
    band_usd = {}
    if min_band is not None and entity_type == "keyword":
        band_usd = {k: json.loads(m).get("attainable_usd") for k, m in con.execute(
            "SELECT entity_id, meta FROM factor_values WHERE metric='attainable' AND entity_type='keyword' AND snapshot_date=?", [d]).fetchall() if m}
    rcfg = cfg.get("reach", {})
    gated, reach_mid, reach_slope = set(rcfg.get("gated", [])), float(rcfg.get("midpoint", 70)), float(rcfg.get("slope", 8))
    max_brand = filters.get("max_brandness")
    min_cov = filters.get("min_coverage")
    latin_only = filters.get("latin_only", True)
    max_dif = filters.get("max_difficulty")
    excl_terms = [t.lower() for t in filters.get("exclude_terms", [])]
    game_sectors: set[str] = set()
    if filters.get("exclude_game_sectors", True) and entity_type == "keyword":
        from ..metrics.base import ensure_sr_latest
        ensure_sr_latest(con, d)
        game_sectors = {k for (k,) in con.execute("""
            WITH a AS (SELECT app_id, primary_genre_id FROM apps WHERE store=? QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1),
                 s AS (SELECT keyword, app_id FROM sr_latest WHERE store=? AND rank<=10
                       AND snapshot_date=(SELECT max(snapshot_date) FROM sr_latest WHERE store=?))
            SELECT keyword FROM s LEFT JOIN a USING (app_id) GROUP BY keyword
            HAVING avg(CASE WHEN primary_genre_id='6014' THEN 1.0 ELSE 0.0 END) >= 0.6""", [store, store, store]).fetchall()}
    rows = []
    for e in mat:
        excluded = []
        if max_conc is not None and (mat[e].get("concentration") or 0) > max_conc:
            excluded.append(f"concentration>{max_conc}")
        if max_brand is not None and e not in labels and (mat[e].get("brandness") or 0) > max_brand:  # heuristic only when no LLM label
            excluded.append(f"brandness>{max_brand}")
        if latin_only and entity_type == "keyword" and not all(ord(ch) < 0x250 for ch in e):
            excluded.append("non-latin script")
        if max_dif is not None and (mat[e].get("difficulty") or 0) > max_dif:
            excluded.append(f"difficulty>{max_dif}")
        if entity_type == "keyword" and require_sector and mat[e].get("breadth") is None:
            excluded.append("thin sector (<min_sector_size relevant apps)")
        if min_build is not None and mat[e].get("buildability") is not None and mat[e]["buildability"] < min_build:
            excluded.append(f"buildability<{min_build}")
        if labels.get(e) in hide:
            excluded.append(f"label:{labels[e]}")
        if min_band is not None and band_usd.get(e) is not None and band_usd[e] < min_band:
            excluded.append(f"band<${min_band}")
        if e in game_sectors:
            excluded.append("games")
        if entity_type == "keyword" and any(t in e for t in excl_terms):
            excluded.append("excluded term")
        score, used, wsum, contrib = 0.0, [], 0.0, {}
        dif = mat[e].get("difficulty")
        reach = 1.0 if dif is None else 1.0 / (1.0 + math.exp((dif - reach_mid) / reach_slope))
        for m, w in weights.items():
            if e in z.get(m, {}):
                c = w * z[m][e]
                c = c * reach if (m in gated and c > 0) else c
                score += c
                contrib[m] = round(c, 2)
                used.append(m)
                wsum += abs(w)
        if spec.get(e) is not None:     # less specific needs lose up to composite_penalty points (specificity 1)
            score -= spec_pen * (5 - max(1, min(5, spec[e]))) / 4
        total_w = sum(abs(w) for w in weights.values()) or 1.0
        coverage = wsum / total_w
        if min_cov is not None and coverage < min_cov:
            excluded.append(f"coverage<{min_cov}")
        rows.append({"entity_type": entity_type, "entity_id": e, "metric": "composite", "value": round(score, 4),
                     "meta": json.dumps({"coverage": round(coverage, 2), "used": used, "excluded": bool(excluded), "label": labels.get(e), "specificity": spec.get(e), "reach": round(reach, 3), "contrib": contrib,
                                         "excluded_reason": ", ".join(excluded) or None}),
                     "snapshot_date": d, "computed_at": now})
    return upsert(con, "factor_values", rows)


def score_all(con, store: str = settings.store, snapshot_date: dt.date | None = None) -> dict:
    counts = run_metrics(con, store, snapshot_date)
    counts["composite_keyword"] = composite(con, store, snapshot_date, "keyword")
    from .index import build_index
    from .topics import build_topic_index
    counts["index"] = build_index(con, store, snapshot_date)
    counts["topics"] = build_topic_index(con, store, snapshot_date)   # topics scored from their members' index
    from .backtest import snapshot_predictions
    counts["predictions"] = snapshot_predictions(con, snapshot_date)
    try:   # fresh read-only copy so the dashboard shows these scores even while a long job holds the write lock
        from ..db import snapshot_copy
        snapshot_copy(con)
    except Exception as e:
        log.warning("snapshot copy failed: %s", e)
    return counts
