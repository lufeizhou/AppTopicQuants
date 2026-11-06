"""Labelling jobs: TypeSafe Jev first, DeepSeek (thinking on) only where Jev is unsure (decided 2026-11-06).

Measured on the same samples, against an answer key (agreements + Claude's judgement of every disagreement):
  job            DeepSeek  Jev alone  this hybrid   sent to DeepSeek
  labels         97%       94%        97%          35%   (need vs not-need; 3-way 93% = 93%)
  brand check    81%       86%        93%          21%
  buildability   93%       83%        91%          44%
Each function returns the same shape as the DeepSeek method it replaces, so callers change one line. Without a
TYPESAFE_API_KEY everything goes to DeepSeek as before.
Labels also ask Jev an adult-intent question; adult searches become rubbish whoever answered (metrics/adult.py).
Jev spend is collected per job here and written to the spend table by record_jev(con), called where the job records
its DeepSeek spend.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("apptopics.judges")
LABEL_MIN_CONF = 0.7          # Jev label confidence below this -> DeepSeek
BRAND_UNSURE = (0.2, 0.8)     # Jev p_brand inside this band -> DeepSeek
BUILD_UNSURE = (0.25, 0.75)   # Jev buildability inside this band -> DeepSeek
STATS: dict[str, dict[str, int]] = {}
_SPEND: dict[str, list] = {}  # job -> [input tokens, usd, calls] not yet written to the spend table
_LOCK = threading.Lock()


def _spent(job: str, jv) -> None:
    with _LOCK:
        s = _SPEND.setdefault(job, [0, 0.0, 0])
        s[0] += jv.usage_in
        s[1] += jv.cost_usd()
        s[2] += jv.calls


def record_jev(con) -> float:
    """Write the Jev spend collected since the last call to the spend table (api 'jev', job 'llm_<job>')."""
    from .. import budget
    with _LOCK:
        pending = dict(_SPEND)
        _SPEND.clear()
    for job, (tok, usd, calls) in pending.items():
        if calls:
            budget.record(con, "jev", f"llm_{job}", tok, usd, f"{calls} calls")
    return sum(v[1] for v in pending.values())


def _jev():
    from ..sources.jev import JevClient
    j = JevClient()
    return j if j.available else None


def _map(fn, items, workers=16):
    def safe(x):
        try:
            return x, fn(x)
        except Exception as e:   # one failed Jev call -> that item goes to DeepSeek
            log.warning("jev %r failed: %r", x, e)
            return x, None
    with ThreadPoolExecutor(workers) as ex:
        return dict(ex.map(safe, items))


def _count(job, jev_n, ds_n):
    s = STATS.setdefault(job, {"jev": 0, "deepseek": 0})
    s["jev"] += jev_n
    s["deepseek"] += ds_n


def label_keywords(items: list[dict], ds) -> dict[str, dict]:
    """items [{term, top_apps}] -> term -> {label, specificity, p_brand, reason} (DeepSeek label_keywords shape)."""
    from ..metrics.adult import is_adult
    jv = _jev()
    out: dict[str, dict] = {}
    rest = items
    adult: dict[str, float] = {}
    if jv:
        res = _map(lambda t: jv.label_term(t, next(i["top_apps"] for i in items if i["term"] == t)), [i["term"] for i in items])
        _spent("labels", jv)
        for t, r in res.items():
            if r:
                adult[t.lower()] = r["p_adult"]
            if r and r["confidence"] >= LABEL_MIN_CONF:
                out[t.lower()] = {"label": r["label"], "specificity": r["specificity"] if r["label"] == "need" else None,
                                  "p_brand": round(r["p_brand"], 3), "reason": f"jev {r['label']} (confidence {r['confidence']:.2f})"}
        rest = [i for i in items if i["term"].lower() not in out]
    if rest:
        out.update(ds.label_keywords(rest))
    for t, v in out.items():
        if v.get("label") != "rubbish" and is_adult(t, adult.get(t)):
            out[t] = {**v, "label": "rubbish", "specificity": None, "reason": f"adult intent (jev {adult[t]:.2f})"}
    _count("labels", len(items) - len(rest), len(rest))
    return out


def brand_check(items: list[dict], ds) -> dict[str, dict]:
    """items [{term, apps}] -> term -> {p_brand, kind, reason} (DeepSeek brand_check shape)."""
    jv = _jev()
    out: dict[str, dict] = {}
    rest = items
    if jv:
        res = _map(lambda t: jv.brand_score(t, next(i["apps"] for i in items if i["term"] == t)), [i["term"] for i in items])
        _spent("brand_check", jv)
        for t, r in res.items():
            if r and not (BRAND_UNSURE[0] <= r["p_brand"] <= BRAND_UNSURE[1]):
                out[t.lower()] = {"p_brand": round(r["p_brand"], 3), "kind": "app" if r["p_brand"] > 0.5 else "none",
                                  "reason": f"jev: names one app {r['p_name']:.2f}, companion utility {r['p_companion']:.2f}"}
        rest = [i for i in items if i["term"].lower() not in out]
    if rest:
        out.update(ds.brand_check(rest))
    _count("brand_check", len(items) - len(rest), len(rest))
    return out


def buildability(keywords: list[str], constraints: str, ds) -> dict[str, dict]:
    """keywords -> kw -> {score, reason} (DeepSeek buildability shape)."""
    jv = _jev()
    out: dict[str, dict] = {}
    rest = keywords
    if jv:
        names = {"lic": "licensed content or data", "net": "network effects", "os": "OS or hardware access", "big": "big game or regulated"}
        res = _map(lambda k: jv.buildability(k, constraints), keywords)
        _spent("buildability", jv)
        for k, r in res.items():
            if r and not (BUILD_UNSURE[0] <= r["score"] <= BUILD_UNSURE[1]):
                out[k.lower()] = {"score": round(r["score"], 3), "reason": f"jev: real need {r['parts']['need']:.2f}, main blocker "
                                  f"{names[r['main_blocker']]} {r['parts'][r['main_blocker']]:.2f}"}
        rest = [k for k in keywords if k.lower() not in out]
    if rest:
        out.update(ds.buildability(rest, constraints))
    _count("buildability", len(keywords) - len(rest), len(rest))
    return out
