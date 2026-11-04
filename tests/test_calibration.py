"""Calibration and backtest arithmetic on synthetic anchors (no network, no database)."""
import math
import random

from apptopics.pipeline.anchor_fit import _hit_table, evaluate, fit_knots, fit_logistic, p_logistic
from apptopics.pipeline.backtest import fit_exponents, model_version


def _anchors(n=400, seed=3):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        rpm = 10 ** rng.uniform(-0.5, 2.5)                       # 0.3 .. 300 ratings per month
        usd = 300 * rpm ** 0.7 * 10 ** rng.gauss(0, 0.5)         # noisy power law
        out.append({"app_id": str(i), "usd": usd, "ratings": int(rpm * 24), "months": 24.0, "rpm": rpm, "price": 0.0})
    return out


def test_fit_knots_is_monotone_and_tracks_the_curve():
    xs, ys = fit_knots(_anchors())
    assert len(xs) >= 5 and ys == sorted(ys)
    # the running median follows 300 * rpm^0.7 within the noise
    for x, y in zip(xs, ys):
        assert abs(math.log10(y) - math.log10(300 * x ** 0.7)) < 0.25


def test_logistic_separates_rich_from_poor():
    rows = _anchors()
    coef = fit_logistic(rows, 5000)
    rich = [p_logistic(coef, r) for r in rows if r["usd"] >= 5000]
    poor = [p_logistic(coef, r) for r in rows if r["usd"] < 1000]
    assert sum(rich) / len(rich) > 3 * sum(poor) / len(poor)
    assert _hit_table([0.9, 0.9, 0.1, 0.4], [1, 0, 1, 1]) == {"flagged": 2, "hits": 1, "recall": 0.33, "precision": 0.5}


def test_evaluate_reports_both_models():
    rows = _anchors()
    knots = fit_knots(rows)
    ev = evaluate(rows, knots, 0.5, fit_logistic(rows, 5000), 5000)
    assert ev["n"] == 400 and ev["curve"]["auc"] > 0.8 and ev["logistic"]["auc"] > 0.8
    assert 0 <= ev["curve"]["brier"] <= 0.25 and ev["curve"]["calibration"]


def test_fit_exponents_recovers_the_dominant_part():
    rng = random.Random(1)
    rows = []
    for _ in range(600):
        m, r, d = rng.uniform(0.05, 1), rng.uniform(0.05, 1), rng.uniform(0.05, 1)
        p = 1 / (1 + math.exp(-(3 * math.log(m) + 1 * math.log(r) + 0.2 * math.log(d) + 1.5)))
        rows.append({"money": m, "reach": r, "demand": d, "winner": 1 if rng.random() < p else 0})
    res = fit_exponents(rows, boots=30)
    assert res["n"] == 600 and res["normalised_exponents"]["money"] > res["normalised_exponents"]["reach"] > res["normalised_exponents"]["demand"]
    assert res["ci90"]["money"][0] > 0
    assert len(model_version()) == 10
