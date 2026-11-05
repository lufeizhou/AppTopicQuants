"""Is the ranking believable? Checks against config/golden.toml plus one data check that needs no labels.

  good_visible   share of 'good' niches not hidden                    target >= 0.8
  good_top_half  share of visible 'good' niches in the top half      (information only)
  junk_top100    'junk' keywords among the top 100 visible            target 0
  label_top100   DeepSeek brand/rubbish or specificity-1 labels in the top 100   target <= 10
  hard_flagged   share of 'hard' markets with difficulty >= 75        target >= 0.8
  grossing_rho   Spearman rank correlation, for apps on the overall top-grossing chart, between chart rank and
                 our rating-based revenue estimate (the estimator used for apps NOT on a chart). target >= 0.5
"""
from __future__ import annotations

import json
import tomllib

from ..config import ROOT, settings
from ..metrics.sector import estimate_app_revenue

# good_top_half is reported but not a pass criterion: "a niche we could build" is not "a niche whose p5-p20
# apps earn $5k"; after calibration several classics (metronome, white noise) are honestly low-paying.
TARGETS = {"good_visible": (">=", 0.8), "junk_top100": ("<=", 0),
           "label_top100": ("<=", 10), "hard_flagged": (">=", 0.8), "grossing_rho": (">=", 0.5)}


def _spearman(x: list[float], y: list[float]) -> float:
    import numpy as np
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1])


def grossing_check(con, store: str = settings.store) -> dict:
    """Within each non-game genre's top-grossing chart, does our rating-based estimate order apps like Apple does?
    Returns the app-weighted mean Spearman rho over genres with >= 30 charting apps."""
    import numpy as np
    cd = con.execute("SELECT max(snapshot_date) FROM chart_entries WHERE store=? AND chart='grossing'", [store]).fetchone()[0]
    rows = con.execute("""
        WITH a AS (SELECT * FROM apps WHERE store=? QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1)
        SELECT c.genre_id, c.rank, a.rating_count, a.rating_count_current, a.release_date, a.current_version_date
        FROM chart_entries c JOIN a USING (app_id)
        WHERE c.store=? AND c.chart='grossing' AND c.genre_id NOT IN ('all', '', '6014') AND c.snapshot_date=?""",
                       [store, store, cd]).fetchall()
    cfg = settings.weights().get("monetization", {})
    by: dict[str, list[tuple[int, float]]] = {}
    for g, rank, rc, rcc, rel, ver in rows:
        est, _ = estimate_app_revenue(cfg, cd, rc, rcc, rel, ver, None)
        if est > 0:
            by.setdefault(g, []).append((rank, est))
    rhos, ws = [], []
    for lst in by.values():
        if len(lst) >= 30:
            rhos.append(_spearman([-r for r, _ in lst], [e for _, e in lst])); ws.append(len(lst))
    if not rhos:
        return {"grossing_rho": None, "grossing_apps": 0}
    return {"grossing_rho": round(float(np.average(rhos, weights=ws)), 3), "grossing_apps": sum(ws), "grossing_genres": len(rhos)}


def validate(con, store: str = settings.store) -> dict:
    g = tomllib.loads((ROOT / "config" / "golden.toml").read_text())
    d = con.execute("SELECT max(snapshot_date) FROM factor_values WHERE metric='composite' AND entity_type='keyword'").fetchone()[0]
    rows = con.execute("""SELECT entity_id, value, meta FROM factor_values WHERE metric='composite' AND entity_type='keyword'
                          AND snapshot_date=? ORDER BY value DESC""", [d]).fetchall()
    meta = {k: json.loads(m) for k, _, m in rows}
    visible = [k for k, _, m in rows if not meta[k].get("excluded")]
    pos = {k: i for i, k in enumerate(visible)}
    diff = dict(con.execute("SELECT entity_id, value FROM factor_values WHERE metric='difficulty' AND entity_type='keyword' AND snapshot_date=?", [d]).fetchall())
    good = [k for k in g["good"] if k in meta]
    good_vis = [k for k in good if k in pos]
    half = len(visible) / 2
    top100 = visible[:100]
    res = {
        "visible": len(visible), "scored": len(rows),
        "good_visible": round(len(good_vis) / max(1, len(good)), 2),
        "good_top_half": round(sum(pos[k] < half for k in good_vis) / max(1, len(good_vis)), 2),
        "junk_top100": sum(k in top100 for k in g["junk"]),
        "label_top100": sum(1 for k in top100 if meta[k].get("label") in ("brand", "rubbish") or (meta[k].get("specificity") or 3) <= 1),
        "hard_flagged": round(sum((diff.get(k) or 0) >= 75 for k in g["hard"] if k in diff) / max(1, sum(k in diff for k in g["hard"])), 2),
        **grossing_check(con, store),
        "good_detail": {k: (pos[k] + 1 if k in pos else meta[k].get("excluded_reason")) for k in good},
        "junk_in_top100": [k for k in g["junk"] if k in top100],
        "missing_from_data": [k for k in g["good"] + g["junk"] + g["hard"] if k not in meta],
    }
    res["pass"] = {m: (res[m] is not None and (res[m] >= t if op == ">=" else res[m] <= t)) for m, (op, t) in TARGETS.items()}
    return res
