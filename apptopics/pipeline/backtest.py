"""Backtest: does the opportunity grade predict what happens next?

Every scoring run snapshots, per visible or graded keyword, the opportunity score and the state of its market
(cycle_id = scoring date). After >= horizon days, `backtest()` compares each old snapshot with today's data:

  newcomer_entry   apps released AFTER the snapshot that now sit in the keyword's top 30 (can newcomers get in?)
  pop_change       Apple popularity now minus then
  band_change      log10 change of the p5-p20 band revenue
and reports, per grade, the mean outcome plus the Spearman rank correlation between score and each outcome.
If A/B niches do not beat C-E on newcomer entry, the weights are wrong and should be revisited."""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np

from ..db import today, upsert


def snapshot_predictions(con, snapshot_date: dt.date | None = None) -> int:
    d = snapshot_date or today()
    rows = con.execute("""
        SELECT o.entity_id, o.value, o.meta, dm.value, atn.meta, ws.value
        FROM factor_values o
        LEFT JOIN factor_values dm ON dm.entity_id=o.entity_id AND dm.metric='demand' AND dm.snapshot_date=o.snapshot_date AND dm.entity_type='keyword'
        LEFT JOIN factor_values atn ON atn.entity_id=o.entity_id AND atn.metric='attainable' AND atn.snapshot_date=o.snapshot_date AND atn.entity_type='keyword'
        LEFT JOIN factor_values ws ON ws.entity_id=o.entity_id AND ws.metric='weak_spots' AND ws.snapshot_date=o.snapshot_date AND ws.entity_type='keyword'
        WHERE o.metric='opp_score' AND o.entity_type='keyword' AND o.snapshot_date=? AND o.value > 0""", [d]).fetchall()
    out = []
    for k, score, _meta, pop, at_meta, ws in rows:
        usd = (json.loads(at_meta) if at_meta else {}).get("attainable_usd")
        for metric, val in (("opp_score", score), ("demand", pop), ("attainable_usd", usd), ("weak_spots", ws)):
            if val is not None:
                out.append({"cycle_id": str(d), "entity_type": "keyword", "entity_id": k, "metric": metric, "predicted": float(val),
                            "horizon_days": 28, "made_at": d, "realized": None, "scored_at": None})
    con.execute("DELETE FROM predictions WHERE cycle_id=?", [str(d)])
    return upsert(con, "predictions", out)


def _spearman(a, b) -> float | None:
    if len(a) < 10:
        return None
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return round(float(np.corrcoef(ra, rb)[0, 1]), 3)


def backtest(con, horizon_days: int = 28) -> dict:
    cycles = [c for (c,) in con.execute("SELECT DISTINCT cycle_id FROM predictions ORDER BY cycle_id").fetchall()]
    due = [c for c in cycles if (today() - dt.date.fromisoformat(c)).days >= horizon_days]
    if not due:
        nxt = (dt.date.fromisoformat(cycles[0]) + dt.timedelta(days=horizon_days)) if cycles else None
        return {"status": "waiting", "cycles": cycles, "first_result_on": str(nxt) if nxt else "after the first scoring run"}
    c = due[-1]
    made = dt.date.fromisoformat(c)
    pred = {}
    for k, m, v in con.execute("SELECT entity_id, metric, predicted FROM predictions WHERE cycle_id=?", [c]).fetchall():
        pred.setdefault(k, {})[m] = v
    pop_now = dict(con.execute("""SELECT entity_id, value FROM factor_values WHERE metric='demand' AND entity_type='keyword'
                                  QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""").fetchall())
    band_now = {k: (json.loads(m) or {}).get("attainable_usd") for k, m in con.execute("""SELECT entity_id, meta FROM factor_values
        WHERE metric='attainable' AND entity_type='keyword' QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY snapshot_date DESC)=1""").fetchall() if m}
    entry = dict(con.execute("""
        WITH s AS (SELECT keyword, app_id FROM search_results WHERE rank<=30 QUALIFY dense_rank() OVER (PARTITION BY keyword ORDER BY snapshot_date DESC)=1),
             a AS (SELECT app_id, release_date FROM apps QUALIFY row_number() OVER (PARTITION BY app_id ORDER BY snapshot_date DESC)=1)
        SELECT keyword, sum(CASE WHEN a.release_date >= ? THEN 1 ELSE 0 END) FROM s LEFT JOIN a USING (app_id) GROUP BY keyword""",
                             [dt.datetime.combine(made, dt.time())]).fetchall())
    rows = []
    for k, p in pred.items():
        if "opp_score" not in p:
            continue
        r = {"keyword": k, "score": p["opp_score"], "newcomer_entry": entry.get(k)}
        if p.get("demand") is not None and pop_now.get(k) is not None:
            r["pop_change"] = pop_now[k] - p["demand"]
        if p.get("attainable_usd") and band_now.get(k):
            r["band_change"] = math.log10(band_now[k] + 1) - math.log10(p["attainable_usd"] + 1)
        rows.append(r)
    grade = lambda s: "A" if s >= 50 else "B" if s >= 38 else "C" if s >= 28 else "D" if s >= 18 else "E"
    by = {}
    for r in rows:
        by.setdefault(grade(r["score"]), []).append(r)
    summary = {g: {"n": len(v), **{o: round(float(np.mean([x[o] for x in v if x.get(o) is not None])), 3)
                                   for o in ("newcomer_entry", "pop_change", "band_change") if any(x.get(o) is not None for x in v)}}
               for g, v in sorted(by.items())}
    corr = {o: _spearman([r["score"] for r in rows if r.get(o) is not None], [r[o] for r in rows if r.get(o) is not None])
            for o in ("newcomer_entry", "pop_change", "band_change")}
    return {"status": "scored", "cycle": c, "days_elapsed": (today() - made).days, "n": len(rows), "by_grade": summary,
            "spearman_score_vs_outcome": corr}
