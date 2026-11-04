"""Backtest: does the opportunity grade predict what happens next?

Every scoring run snapshots, per graded keyword (cycle_id = scoring date):
  predictions     opp_score and its parts (money, reach, demand, conc_adj, build_adj, trend_adj), popularity,
                  band revenue and weak spots
  backtest_apps   the young apps (released <= 12 months before) that serve the keyword, with their rating count then
  backtest_cycles the model version (hash of config/weights.toml + the score formula) so cycles made by different
                  models are never mixed
After >= horizon days `backtest()` measures, per keyword, what really happened:
  winner          a young app of the sector gained ratings at a pace the calibrated curve maps to >= target/month
                  (P >= 0.5), or entered a grossing chart: the outcome the model claims to predict
  p_winner        the highest such probability among the keyword's young apps (a soft version of the same)
  newcomer_entry  apps released after the snapshot now in the top 30 (can newcomers get in at all?)
  pop_change      Apple popularity now minus then
and reports them per grade, plus a logistic fit of `winner` on log(money), log(reach), log(demand): the fitted
coefficients are the exponents the data supports (the formula uses 0.45 / 0.35 / 0.20), with bootstrap intervals.
If A/B do not beat C-E on winners, the weights are wrong and should be revisited."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math

import numpy as np

from ..config import settings
from ..db import today, upsert

HORIZON_DAYS = 90
YOUNG_DAYS = 365
FORMULA = "100*money^0.45*reach^0.35*demand^0.20*conc*build*trend"
PART_METRICS = ("money", "reach", "demand", "conc_adj", "build_adj", "trend_adj")


def model_version() -> str:
    h = hashlib.sha1(settings.weights_path.read_bytes() + FORMULA.encode()).hexdigest()[:10]
    return h


def snapshot_predictions(con, snapshot_date: dt.date | None = None, store: str = settings.store) -> int:
    from ..metrics.sector import sector_estimates
    d = snapshot_date or today()
    rows = con.execute("""
        SELECT o.entity_id, o.value, o.meta, dm.value, atn.meta, ws.value
        FROM factor_values o
        LEFT JOIN factor_values dm ON dm.entity_id=o.entity_id AND dm.metric='demand' AND dm.snapshot_date=o.snapshot_date AND dm.entity_type='keyword'
        LEFT JOIN factor_values atn ON atn.entity_id=o.entity_id AND atn.metric='attainable' AND atn.snapshot_date=o.snapshot_date AND atn.entity_type='keyword'
        LEFT JOIN factor_values ws ON ws.entity_id=o.entity_id AND ws.metric='weak_spots' AND ws.snapshot_date=o.snapshot_date AND ws.entity_type='keyword'
        WHERE o.metric='opp_score' AND o.entity_type='keyword' AND o.snapshot_date=? AND o.value > 0""", [d]).fetchall()
    out, apps_out = [], []
    sectors = sector_estimates(con, store, d)
    cutoff = dt.datetime.combine(d, dt.time()) - dt.timedelta(days=YOUNG_DAYS)
    for k, score, meta, pop, at_meta, ws in rows:
        m = json.loads(meta) if meta else {}
        parts = m.get("parts") or {}
        usd = (json.loads(at_meta) if at_meta else {}).get("attainable_usd")
        vals = [("opp_score", score), ("demand", pop), ("attainable_usd", usd), ("weak_spots", ws)] + [(p, parts.get(p)) for p in PART_METRICS]
        for metric, val in vals:
            if val is not None:
                out.append({"cycle_id": str(d), "entity_type": "keyword", "entity_id": k, "metric": metric, "predicted": float(val),
                            "horizon_days": HORIZON_DAYS, "made_at": d, "realized": None, "scored_at": None})
        for a in sectors.get(k, []):
            if a.release_date is not None and a.release_date >= cutoff:
                apps_out.append({"cycle_id": str(d), "keyword": k, "app_id": a.app_id, "name": a.name, "rating_count": int(a.rating_count or 0),
                                 "est_then": round(a.est), "release_date": a.release_date, "made_at": d})
    con.execute("DELETE FROM predictions WHERE cycle_id=?", [str(d)])
    con.execute("DELETE FROM backtest_apps WHERE cycle_id=?", [str(d)])
    n = upsert(con, "predictions", out)
    upsert(con, "backtest_apps", apps_out)
    upsert(con, "backtest_cycles", [{"cycle_id": str(d), "made_at": d, "model_version": model_version(), "n_keywords": len(rows),
                                     "n_young_apps": len(apps_out)}])
    return n


def _spearman(a, b) -> float | None:
    if len(a) < 10:
        return None
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return round(float(np.corrcoef(ra, rb)[0, 1]), 3)


def _logit(X: np.ndarray, y: np.ndarray, l2: float = 0.5, iters: int = 50) -> np.ndarray:
    w = np.zeros(X.shape[1]); reg = l2 * np.eye(X.shape[1]); reg[0, 0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) + reg @ w
        H = (X * (p * (1 - p))[:, None]).T @ X + reg
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def fit_exponents(rows: list[dict], boots: int = 200, seed: int = 1) -> dict | None:
    """Logistic regression of `winner` on log(money), log(reach), log(demand). Coefficients are comparable with the
    formula's exponents up to a common scale; the ratios are what matters."""
    use = [r for r in rows if r.get("winner") is not None and all(r.get(p) for p in ("money", "reach", "demand"))]
    if len(use) < 60 or sum(r["winner"] for r in use) < 8:
        return None
    X = np.array([[1.0, math.log(r["money"]), math.log(r["reach"]), math.log(r["demand"])] for r in use])
    y = np.array([float(r["winner"]) for r in use])
    w = _logit(X, y)
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(boots):
        idx = rng.integers(0, len(use), len(use))
        if y[idx].sum() >= 3:
            bs.append(_logit(X[idx], y[idx]))
    bs = np.array(bs) if bs else np.array([w])
    names = ("money", "reach", "demand")
    coef = {n: round(float(w[i + 1]), 3) for i, n in enumerate(names)}
    tot = sum(max(0.0, v) for v in coef.values()) or 1.0
    return {"n": len(use), "winners": int(y.sum()), "coef": coef,
            "ci90": {n: [round(float(np.percentile(bs[:, i + 1], 5)), 3), round(float(np.percentile(bs[:, i + 1], 95)), 3)] for i, n in enumerate(names)},
            "normalised_exponents": {n: round(max(0.0, v) / tot, 2) for n, v in coef.items()},
            "formula_exponents": {"money": 0.45, "reach": 0.35, "demand": 0.20}}


def outcomes(con, cycle: str, store: str = settings.store) -> list[dict]:
    """Per keyword of a cycle: predicted score and parts, and what happened since."""
    from ..metrics.sector import p_above, rpm_to_usd
    made = dt.date.fromisoformat(cycle)
    cfg = settings.weights().get("monetization", {})
    target = float(settings.weights().get("target", {}).get("monthly_usd", 5000))
    pred: dict[str, dict] = {}
    for k, m, v in con.execute("SELECT entity_id, metric, predicted FROM predictions WHERE cycle_id=?", [cycle]).fetchall():
        pred.setdefault(k, {})[m] = v
    pop_now = dict(con.execute("""SELECT entity_id, value FROM factor_values WHERE metric='demand' AND entity_type='keyword'
                                  QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""").fetchall())
    entry = dict(con.execute("""
        WITH s AS (SELECT keyword, app_id FROM search_results WHERE rank<=30 QUALIFY dense_rank() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1),
             a AS (SELECT app_id, release_date FROM apps QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1)
        SELECT keyword, sum(CASE WHEN a.release_date >= ? THEN 1 ELSE 0 END) FROM s LEFT JOIN a USING (app_id) GROUP BY keyword""",
                             [dt.datetime.combine(made, dt.time())]).fetchall())
    # young apps then -> ratings gained since, and chart presence now
    young = con.execute("""
        WITH now AS (SELECT app_id, rating_count, snapshot_date FROM apps WHERE store=? QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1),
             ch AS (SELECT DISTINCT app_id FROM chart_entries WHERE store=? AND chart='grossing'
                    AND snapshot_date=(SELECT max(snapshot_date) FROM chart_entries WHERE store=? AND chart='grossing'))
        SELECT b.keyword, b.app_id, b.name, b.rating_count, now.rating_count, now.snapshot_date, ch.app_id IS NOT NULL
        FROM backtest_apps b LEFT JOIN now USING (app_id) LEFT JOIN ch USING (app_id) WHERE b.cycle_id=?""", [store, store, store, cycle]).fetchall()
    best: dict[str, dict] = {}
    for k, _app_id, name, rc0, rc1, d1, charted in young:
        days = (d1 - made).days if d1 else 0
        if rc1 is None or days < 7:
            continue
        rpm = max(0, rc1 - rc0) / days * 30.4
        p = 1.0 if charted else p_above(cfg, rpm_to_usd(cfg, rpm), target)
        cur = best.get(k)
        if cur is None or p > cur["p"]:
            best[k] = {"p": p, "app": name, "gained": rc1 - rc0, "days": days}
    rows = []
    for k, p in pred.items():
        if "opp_score" not in p:
            continue
        r = {"keyword": k, "score": p["opp_score"], "newcomer_entry": entry.get(k), **{m: p.get(m) for m in PART_METRICS}}
        if p.get("demand") is not None and pop_now.get(k) is not None:
            r["pop_change"] = pop_now[k] - p["demand"]
        b = best.get(k)
        r["p_winner"] = round(b["p"], 3) if b else (0.0 if k in {x[0] for x in young} else None)
        r["winner"] = (1 if b["p"] >= 0.5 else 0) if b else (0 if r["p_winner"] == 0.0 else None)
        r["best_young"] = b
        rows.append(r)
    return rows


def backtest(con, horizon_days: int = HORIZON_DAYS, store: str = settings.store) -> dict:
    cycles = [c for (c,) in con.execute("SELECT DISTINCT cycle_id FROM predictions ORDER BY cycle_id").fetchall()]
    versions = dict(con.execute("SELECT cycle_id, model_version FROM backtest_cycles").fetchall())
    due = [c for c in cycles if (today() - dt.date.fromisoformat(c)).days >= horizon_days]
    if not due:
        nxt = (dt.date.fromisoformat(cycles[0]) + dt.timedelta(days=horizon_days)) if cycles else None
        return {"status": "waiting", "cycles": cycles, "horizon_days": horizon_days, "model_versions": versions,
                "first_result_on": str(nxt) if nxt else "after the first scoring run"}
    c = due[-1]
    rows = outcomes(con, c, store)
    grade = lambda s: "A" if s >= 50 else "B" if s >= 38 else "C" if s >= 28 else "D" if s >= 18 else "E"
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(grade(r["score"]), []).append(r)
    outs = ("winner", "p_winner", "newcomer_entry", "pop_change")
    summary = {g: {"n": len(v), **{o: round(float(np.mean([x[o] for x in v if x.get(o) is not None])), 3)
                                   for o in outs if any(x.get(o) is not None for x in v)}} for g, v in sorted(by.items())}
    corr = {o: _spearman([r["score"] for r in rows if r.get(o) is not None], [r[o] for r in rows if r.get(o) is not None]) for o in outs}
    ab = [r for r in rows if grade(r["score"]) in "AB" and r.get("winner") is not None]
    rest = [r for r in rows if grade(r["score"]) not in "AB" and r.get("winner") is not None]
    verdict = None
    if ab and rest:
        wa, wr = np.mean([r["winner"] for r in ab]), np.mean([r["winner"] for r in rest])
        verdict = {"ab_winner_share": round(float(wa), 3), "cde_winner_share": round(float(wr), 3), "lift": round(float(wa / wr), 2) if wr else None,
                   "reading": "A/B beat the rest: the weights point the right way" if wr and wa / wr >= 1.5 else
                              "A/B do not clearly beat the rest: revisit the weights (see fitted exponents)"}
    winners = sorted([r for r in rows if r.get("winner")], key=lambda r: -r["score"])[:15]
    return {"status": "scored", "cycle": c, "model_version": versions.get(c), "days_elapsed": (today() - dt.date.fromisoformat(c)).days,
            "n": len(rows), "n_with_outcome": sum(1 for r in rows if r.get("winner") is not None), "by_grade": summary,
            "spearman_score_vs_outcome": corr, "verdict": verdict, "fitted_exponents": fit_exponents(rows),
            "examples_winners": [{"keyword": r["keyword"], "score": r["score"], **(r["best_young"] or {})} for r in winners]}
