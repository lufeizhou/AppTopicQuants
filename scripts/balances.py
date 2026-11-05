#!/usr/bin/env python3
"""Remaining balance on the paid APIs: ScrapeCreators, DataForSEO, DeepSeek.

    scripts/balances.sh   (or .venv/bin/python scripts/balances.py)

Each check is appended to data/balances.csv, so the table also shows the change since
the previous check and over the last 24 hours. Balance lookups are free on all three.
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


def delta(history: list[dict], name: str, now_bal: float, since: dt.datetime | None) -> float | None:
    """Change from the last logged value (since=None) or the oldest value within the window."""
    rows = [r for r in history if r["service"] == name]
    if since is not None:
        rows = [r for r in rows if dt.datetime.fromisoformat(r["ts"]) >= since]
        ref = rows[0] if rows else None
    else:
        ref = rows[-1] if rows else None
    return None if ref is None else now_bal - float(ref["balance"])


def fmt_delta(d: float | None, unit: str) -> str:
    if d is None:
        return f"{DIM}—{RESET}"
    if abs(d) < 1e-9:
        return f"{DIM}0{RESET}"
    s = f"{d:+,.0f}" if unit == "credits" else f"{d:+,.2f}"
    return f"{RED if d < 0 else GREEN}{s}{RESET}"


def check() -> str:
    now = dt.datetime.now()
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

    lines = [f"{BOLD}API balances{RESET}  {DIM}{now:%Y-%m-%d %H:%M:%S}{RESET}", ""]
    lines.append(f"{'service':<16}{'balance':>14}  {'unit':<8}{'since last':>12}{'24h':>12}  note")
    lines.append(DIM + "─" * 84 + RESET)
    for n, (bal, note, err) in results.items():
        unit, low = SERVICES[n]
        if err:
            lines.append(f"{n:<16}{RED}{'error':>14}{RESET}  {'':<8}{'':>12}{'':>12}  {RED}{err}{RESET}")
            continue
        colour = RED if bal < low else YELLOW if bal < 2 * low else GREEN
        val = f"{bal:,.0f}" if unit == "credits" else f"{bal:,.2f}"
        d_last = fmt_delta(delta(history, n, bal, None), unit)
        d_day = fmt_delta(delta(history, n, bal, now - dt.timedelta(hours=24)), unit)
        # ANSI codes take width; pad the visible text by hand
        pad = lambda s, w: " " * max(0, w - len(_strip(s))) + s
        warn = f"  {RED}LOW (< {low:g}){RESET}" if bal < low else ""
        lines.append(f"{n:<16}{colour}{val:>14}{RESET}  {unit:<8}{pad(d_last, 12)}{pad(d_day, 12)}  {DIM}{note}{RESET}{warn}")
    return "\n".join(lines)


def _strip(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


if __name__ == "__main__":
    print(check())
