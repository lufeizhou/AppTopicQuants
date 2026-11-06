"""Spend accounting and monthly caps for paid APIs.

- record(api, job, units, usd): append actual cost (from API usage/cost fields).
- month_to_date(api): sum of this month's spend.
- estimate_job(api, job, n): pre-run estimate from unit prices in config/budget.toml.
- check(api, est_usd): (ok, reason). ok=False when est + mtd > cap or total cap.
- warn(msg): write to warnings table, log, and raise a macOS notification.
"""
from __future__ import annotations

import datetime as dt
import logging
import subprocess
import tomllib

from .config import ROOT

log = logging.getLogger("apptopics.budget")
BUDGET_PATH = ROOT / "config" / "budget.toml"

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
  ts TIMESTAMP, api TEXT, job TEXT, units DOUBLE, usd DOUBLE, note TEXT);
CREATE TABLE IF NOT EXISTS warnings (
  ts TIMESTAMP, kind TEXT, api TEXT, message TEXT, acknowledged BOOLEAN DEFAULT false);
"""


def load() -> dict:
    with open(BUDGET_PATH, "rb") as f:
        return tomllib.load(f)


def save_caps(caps: dict[str, float], cadence: dict[str, int] | None = None) -> None:
    """Rewrite [caps] (and optionally [cadence]) in budget.toml, keeping comments and unit prices."""
    text = BUDGET_PATH.read_text()
    import re
    for k, v in caps.items():
        text, n = re.subn(rf"^{k} = [^#\n]*?(\s*#.*)?$", lambda m: f"{k} = {float(v):g}" + (m.group(1) or ""), text, count=1, flags=re.M)
        if not n:
            text = text.replace("[caps]\n", f"[caps]\n{k} = {float(v):g}\n", 1)
    for k, v in (cadence or {}).items():
        text = re.sub(rf"^{k} = [^#\n]*?(\s*#.*)?$", lambda m: f"{k} = {int(v)}" + (m.group(1) or ""), text, count=1, flags=re.M)
    BUDGET_PATH.write_text(text)


def ensure_schema(con) -> None:
    try:
        con.execute(SCHEMA)
    except Exception:  # read-only connection: tables either exist already or spend is zero
        pass


def record(con, api: str, job: str, units: float, usd: float, note: str | None = None) -> None:
    ensure_schema(con)
    con.execute("INSERT INTO spend VALUES (?,?,?,?,?,?)", [dt.datetime.now(), api, job, units, usd, note])


def month_to_date(con, api: str | None = None) -> float:
    ensure_schema(con)
    if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='spend'").fetchone()[0]:
        return 0.0
    start = dt.date.today().replace(day=1)
    if api:
        return float(con.execute("SELECT coalesce(sum(usd),0) FROM spend WHERE api=? AND ts>=?", [api, start]).fetchone()[0])
    return float(con.execute("SELECT coalesce(sum(usd),0) FROM spend WHERE ts>=?", [start]).fetchone()[0])


def deepseek_peak(at: dt.datetime | None = None) -> bool:
    """DeepSeek bills double on weekdays in [unit_prices] deepseek_peak_hours, Beijing time (weekends off-peak)."""
    from zoneinfo import ZoneInfo
    bj = ZoneInfo("Asia/Shanghai")
    t = dt.datetime.now(bj) if at is None else (at if at.tzinfo else at.astimezone()).astimezone(bj)   # naive = local time
    if t.weekday() >= 5:
        return False
    for span in str(load().get("unit_prices", {}).get("deepseek_peak_hours", "09-12,14-18")).split(","):
        a, b = (int(x) for x in span.split("-"))
        if a <= t.hour < b:
            return True
    return False


def deepseek_usd_per_m(at: dt.datetime | None = None) -> dict[str, float]:
    """USD per million tokens now (or at `at`): miss / hit / out, peak or off-peak."""
    p = load().get("unit_prices", {})
    k = float(p.get("deepseek_peak_mult", 2.0)) if deepseek_peak(at) else 1.0
    fx = float(p.get("cny_per_usd", 7.1))
    return {"miss": float(p.get("deepseek_cny_miss", 1.0)) * k / fx, "hit": float(p.get("deepseek_cny_hit", 0.02)) * k / fx,
            "out": float(p.get("deepseek_cny_out", 4.0)) * k / fx, "peak": deepseek_peak(at)}


def deepseek_offpeak_note() -> str:
    """One line for the run buttons: is DeepSeek at peak price now, and when does it end."""
    from zoneinfo import ZoneInfo
    if not deepseek_peak():
        return "DeepSeek is at off-peak price now (half of peak)."
    t = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    ends = [int(s.split("-")[1]) for s in str(load().get("unit_prices", {}).get("deepseek_peak_hours", "09-12,14-18")).split(",")
            if int(s.split("-")[0]) <= t.hour < int(s.split("-")[1])]
    return f"DeepSeek is at PEAK price now (2x) until {ends[0]:02d}:00 Beijing time; off-peak is half."


def estimate_job(api: str, job: str, n: int) -> float:
    """Rough pre-run cost estimate for n items of a job."""
    p = load().get("unit_prices", {})
    if api == "deepseek":
        tok_in = {"llm_brands": 20, "llm_labels": 45, "llm_buildability": 60, "topics": 80, "llm_pain_points": 9000, "llm_votes": 1500, "llm_relevance": 1500}.get(job, 500) * n
        tok_out = {"llm_brands": 15, "llm_labels": 30, "llm_buildability": 40, "topics": 40, "llm_pain_points": 400, "llm_votes": 400, "llm_relevance": 250}.get(job, 200) * n
        u = deepseek_usd_per_m()
        return tok_in / 1e6 * u["miss"] + tok_out / 1e6 * u["out"]
    if api == "dataforseo":
        if job == "volume":
            return -(-n // 1000) * p.get("dataforseo_volume_task", 0.06)
        if job == "trends":
            return -(-n // 4) * p.get("dataforseo_trends_task", 0.011)
        if job in ("search", "play_search"):
            return n * p.get("dataforseo_search_serp", 0.0024)
        if job == "play_reviews":
            return n * p.get("dataforseo_play_reviews", 0.00075)
    if api == "jev":   # n = search x app pairs; ~70 input tokens each (question + share of the page state)
        return n * 70 / 1e6 * p.get("jev_in_per_m", 0.042)
    if api == "scrapecreators":
        return n * p.get("scrapecreators_credit", 0.00188)
    return 0.0


def check(con, api: str, est_usd: float) -> tuple[bool, str]:
    caps = load().get("caps", {})
    cap, total_cap = caps.get(api), caps.get("total")
    mtd, mtd_all = month_to_date(con, api), month_to_date(con)
    if cap is not None and mtd + est_usd > cap:
        return False, f"{api}: month-to-date ${mtd:.2f} + est ${est_usd:.2f} > cap ${cap:.2f}"
    if total_cap is not None and mtd_all + est_usd > total_cap:
        return False, f"total: month-to-date ${mtd_all:.2f} + est ${est_usd:.2f} > total cap ${total_cap:.2f}"
    return True, f"{api}: mtd ${mtd:.2f} + est ${est_usd:.2f} within cap ${cap if cap is not None else '-'}"


def warn(con, kind: str, api: str, message: str, notify: bool = True) -> None:
    ensure_schema(con)
    con.execute("INSERT INTO warnings (ts, kind, api, message, acknowledged) VALUES (?,?,?,?,false)",
                [dt.datetime.now(), kind, api, message])
    log.warning("%s %s: %s", kind, api, message)
    if notify:
        try:
            subprocess.run(["osascript", "-e", f'display notification "{message[:200]}" with title "AppTopicsQuants budget"'],
                           timeout=5, capture_output=True)
        except Exception:
            pass


def guard(con, api: str, job: str, n: int) -> bool:
    """Estimate, check the cap, warn and return False if the job must be skipped."""
    est = estimate_job(api, job, n)
    ok, reason = check(con, api, est)
    if not ok:
        warn(con, "cap", api, f"skipped {job} ({n} items): {reason}. Raise the cap in the dashboard Budget panel or top up.")
    else:
        log.info("budget ok: %s %s", job, reason)
    return ok


def plan(con, counts: dict[tuple[str, str], int]) -> list[dict]:
    """Pre-run report: for each (api, job) -> estimate, month-to-date, cap, ok."""
    caps = load().get("caps", {})
    out = []
    for (api, job), n in counts.items():
        est = estimate_job(api, job, n)
        ok, reason = check(con, api, est)
        out.append({"api": api, "job": job, "items": n, "est_usd": round(est, 3), "mtd_usd": round(month_to_date(con, api), 3),
                    "cap_usd": caps.get(api), "ok": ok, "reason": reason})
    if any(api == "deepseek" for api, _ in counts):
        out.append({"api": "deepseek", "job": "price now", "items": 0, "est_usd": 0.0, "mtd_usd": round(month_to_date(con, "deepseek"), 3),
                    "cap_usd": caps.get("deepseek"), "ok": True, "reason": deepseek_offpeak_note()})
    out.append({"api": "total", "job": "", "items": sum(counts.values()), "est_usd": round(sum(r["est_usd"] for r in out), 3),
                "mtd_usd": round(month_to_date(con), 3), "cap_usd": caps.get("total"),
                "ok": month_to_date(con) + sum(r["est_usd"] for r in out) <= (caps.get("total") or 1e9), "reason": ""})
    return out
