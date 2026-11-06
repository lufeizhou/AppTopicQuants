#!/usr/bin/env python3
"""Remaining balance on the paid APIs: ScrapeCreators, DataForSEO, DeepSeek, plus TypeSafe Jev
spend (Jev has no balance API, so its row reads the project's own `spend` table).

    scripts/balances.sh   (or .venv/bin/python scripts/balances.py)

Each check is appended to data/balances.csv, so the table also shows what was spent
today and over the last 7 days (top-ups are not netted against spend). Balance lookups are free on all three.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "balances.csv"
load_dotenv(ROOT / ".env")

# name -> (unit, low-balance warning threshold)
SERVICES = {
    "ScrapeCreators": ("credits", 200),
    "DataForSEO": ("USD", 5.0),
    "DeepSeek": ("CNY", 20.0),
}

GREEN, YELLOW, RED, DIM, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def scrapecreators() -> tuple[float, str]:
    key = os.environ.get("SCRAPECREATORS_API_KEY")
    if not key:
        raise RuntimeError("SCRAPECREATORS_API_KEY not set")
    r = httpx.get("https://api.scrapecreators.com/v1/credit-balance", headers={"x-api-key": key}, timeout=20)
    r.raise_for_status()
    return float(r.json()["creditCount"]), ""


def dataforseo() -> tuple[float, str]:
    login, pw = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if not (login and pw):
        raise RuntimeError("DATAFORSEO_LOGIN / PASSWORD not set")
    r = httpx.get("https://api.dataforseo.com/v3/appendix/user_data", auth=(login, pw), timeout=20)
    r.raise_for_status()
    money = r.json()["tasks"][0]["result"][0]["money"]
    return float(money["balance"]), f"of ${money.get('total', 0):g} deposited"


def deepseek() -> tuple[float, str]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/").removesuffix("/v1")
    r = httpx.get(f"{base}/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    info = j["balance_infos"][0]
    SERVICES["DeepSeek"] = (info["currency"], SERVICES["DeepSeek"][1])
    note = "" if j.get("is_available") else "NOT AVAILABLE"
    if float(info.get("granted_balance", 0)):
        note = f"{info['granted_balance']} granted " + note
    return float(info["total_balance"]), note.strip()


FETCHERS = {"ScrapeCreators": scrapecreators, "DataForSEO": dataforseo, "DeepSeek": deepseek}


def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    with LOG.open() as f:
        return list(csv.DictReader(f))


def append_log(ts: str, results: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "service", "balance"])
        for name, (bal, _note, err) in results.items():
            if err is None:
                w.writerow([ts, name, bal])


def flows(history: list[dict], name: str, now: dt.datetime, now_bal: float,
          since: dt.datetime) -> tuple[float, float, dt.datetime] | None:
    """(spent, topped_up, start) since `since`. Starts from the last value logged before
    `since` (the balance at that moment), else the oldest one inside the window. Every drop
    between checks counts as spend and every rise as a top-up, so a top-up never hides spend."""
    rows = [(dt.datetime.fromisoformat(r["ts"]), float(r["balance"])) for r in history if r["service"] == name]
    before = [p for p in rows if p[0] < since]
    points = before[-1:] + [p for p in rows if p[0] >= since] + [(now, now_bal)]
    if len(points) < 2:
        return None
    spent = topped = 0.0
    for (_, a), (_, b) in zip(points, points[1:]):
        if b < a:
            spent += a - b
        else:
            topped += b - a
    return spent, topped, points[0][0]


def jev_spend(midnight: dt.datetime, week: dt.datetime) -> tuple[float, float]:
    """TypeSafe Jev has no balance API, so read its spend (USD) as recorded by the project's
    own runs in the `spend` table: (today, last 7 days)."""
    import sys
    sys.path.insert(0, str(ROOT))
    from apptopics.db import connect_read
    con, _ = connect_read()
    try:
        if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='spend'").fetchone()[0]:
            return 0.0, 0.0
        q = "SELECT coalesce(sum(usd), 0) FROM spend WHERE api = 'jev' AND ts >= ?"
        return tuple(float(con.execute(q, [t]).fetchone()[0]) for t in (midnight, week))
    finally:
        con.close()


def num(x: float, unit: str) -> str:
    if unit == "credits":
        return f"{x:,.0f}"
    if unit == "USD (Jev)":   # Jev runs cost fractions of a cent
        return f"{x:,.2f}" if x >= 0.995 else f"{x:.3g}"
    return f"{x:,.2f}"


def fmt_spent(f: tuple | None, unit: str) -> str:
    if f is None:
        return f"{DIM}—{RESET}"
    if f[0] < 1e-9:
        return f"{DIM}0{RESET}"
    return f"{RED}-{num(f[0], unit)}{RESET}"


def check() -> str:
    now = dt.datetime.now()  # local time, same as the timestamps in the log
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - dt.timedelta(days=7)
    history = read_log()
    with ThreadPoolExecutor(3) as ex:
        futs = {n: ex.submit(f) for n, f in FETCHERS.items()}
    results = {}
    for n, fut in futs.items():
        try:
            bal, note = fut.result()
            results[n] = (bal, note, None)
        except Exception as e:
            results[n] = (None, "", str(e)[:70])
    append_log(now.isoformat(timespec="seconds"), results)

    # ANSI codes take width; pad the visible text by hand
    pad = lambda s, w: " " * max(0, w - len(_strip(s))) + s
    lines = [f"{BOLD}API balances{RESET}  {DIM}{now:%Y-%m-%d %H:%M:%S}{RESET}", ""]
    lines.append(f"{'service':<16}{'balance':>14}  {'unit':<8}{'today':>10}{'7 days':>10}")
    lines.append(DIM + "─" * 58 + RESET)
    for n, (bal, note, err) in results.items():
        unit, low = SERVICES[n]
        if err:
            lines.append(f"{n:<16}{RED}{'error':>14}{RESET}  {'':<28}  {RED}{err}{RESET}")
            continue
        colour = RED if bal < low else YELLOW if bal < 2 * low else GREEN
        f_day, f_week = (flows(history, n, now, bal, t) for t in (midnight, week))
        lines.append(f"{n:<16}{colour}{num(bal, unit):>14}{RESET}  {unit:<8}"
                     f"{pad(fmt_spent(f_day, unit), 10)}{pad(fmt_spent(f_week, unit), 10)}")
    try:
        j_day, j_week = jev_spend(midnight, week)
        lines.append(f"{'TypeSafe Jev':<16}{DIM}{'n/a':>14}{RESET}  {'USD':<8}"
                     f"{pad(fmt_spent((j_day, 0, None), 'USD (Jev)'), 10)}{pad(fmt_spent((j_week, 0, None), 'USD (Jev)'), 10)}")
    except Exception as e:
        lines.append(f"{'TypeSafe Jev':<16}{RED}{'error':>14}{RESET}  {'':<28}  {RED}{str(e)[:70]}{RESET}")
    lines += ["", f"{DIM}today / 7 days = spent (top-ups excluded) · "
                  f"balance turns yellow / red when running low{RESET}",
              f"{DIM}Jev: no balance API; spend is what this project's runs recorded{RESET}"]
    return "\n".join(lines)


def _strip(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


if __name__ == "__main__":
    print(check())
