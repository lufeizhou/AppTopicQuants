"""DataForSEO: Google Ads search volume + CPC (absolute, 12-month history) and Google
Trends explore (relative, 5-year history, compared against a fixed reference term so
values are comparable across batches). Prepaid, pay-per-task; gated on DATAFORSEO_LOGIN."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field

import httpx

from ..registry import sources

BASE = "https://api.dataforseo.com/v3"
US, EN = 2840, "en"


@sources.register("dataforseo")
@dataclass
class DataForSEOClient:
    login: str = field(default_factory=lambda: os.environ.get("DATAFORSEO_LOGIN", ""))
    password: str = field(default_factory=lambda: os.environ.get("DATAFORSEO_PASSWORD", ""))
    timeout: float = 120.0
    spent_usd: float = 0.0
    tasks: int = 0

    @property
    def available(self) -> bool:
        return bool(self.login and self.password)

    def _post(self, path: str, payload: list[dict]) -> list[dict]:
        import time as _t
        if "google_ads" in path:      # Google Ads endpoints allow ~12 requests/min per account
            _t.sleep(max(0.0, getattr(self, "_last_ads", 0) + 5.5 - _t.time()))
            self._last_ads = _t.time()
        wait = 10.0
        for attempt in range(6):
            r = httpx.post(f"{BASE}{path}", json=payload, auth=(self.login, self.password), timeout=self.timeout)
            if r.status_code in (403, 429) and attempt < 5:   # rate limited: back off and retry
                _t.sleep(wait); wait = min(120.0, wait * 2)
                continue
            break
        r.raise_for_status()
        data = r.json()
        if data.get("status_code") != 20000:
            raise RuntimeError(f"dataforseo {path}: {data.get('status_message')}")
        out = []
        self.spent_usd += float(data.get("cost") or 0)
        for t in data.get("tasks", []):
            self.tasks += 1
            if t.get("status_code") != 20000:
                raise RuntimeError(f"dataforseo task: {t.get('status_message')}")
            out.extend(t.get("result") or [])
        return out

    def search_volume(self, keywords: list[str], location_code: int = US, language: str = EN) -> list[dict]:
        """Google Ads keyword stats for up to 1000 keywords per task."""
        rows: list[dict] = []
        for i in range(0, len(keywords), 1000):
            res = self._post("/keywords_data/google_ads/search_volume/live",
                             [{"keywords": keywords[i:i + 1000], "location_code": location_code, "language_code": language}])
            rows.extend(res)
        return rows

    def trends(self, keywords: list[str], reference: str, years: int = 5, location_code: int = US, language: str = EN) -> list[dict]:
        """Google Trends web interest. Each task holds the reference term + up to 4 keywords.
        Returns [{keyword, points:[(date, value)], rel_to_reference, slope_12m}]."""
        today = dt.date.today()
        out: list[dict] = []
        for i in range(0, len(keywords), 4):
            batch = [reference, *keywords[i:i + 4]]
            res = self._post("/keywords_data/google_trends/explore/live",
                             [{"keywords": batch, "location_code": location_code, "language_code": language, "type": "web",
                               "date_from": (today - dt.timedelta(days=365 * years)).isoformat(), "date_to": today.isoformat()}])
            for r in res:
                for item in r.get("items") or []:
                    if item.get("type") != "google_trends_graph":
                        continue
                    kws = item.get("keywords") or batch
                    series = {k: [] for k in kws}
                    for p in item.get("data") or []:
                        vals = p.get("values") or []
                        for k, v in zip(kws, vals):
                            series[k].append((p.get("date_from"), v if v is not None else 0))
                    out.extend(summarize_trend(k, series[k], series.get(reference, []), reference) for k in kws if k != reference)
        return out


def _get(self: DataForSEOClient, path: str) -> dict:
    r = httpx.get(f"{BASE}{path}", auth=(self.login, self.password), timeout=self.timeout)
    r.raise_for_status()
    return r.json()


def app_searches(self: DataForSEOClient, keywords: list[str], depth: int = 100, priority: int = 2,
                 location_code: int = US, language: str = EN, poll_seconds: float = 5.0, timeout_s: float = 900.0):
    """Yield (keyword, items) for App Store SERPs. Posts up to 100 tasks per request, then polls
    tasks_ready and fetches each advanced result. priority=2 is the ~1 minute queue."""
    import time as _t
    pending: dict[str, str] = {}
    for i in range(0, len(keywords), 100):
        payload = [{"keyword": k, "location_code": location_code, "language_code": language, "depth": depth,
                    "priority": priority, "tag": k} for k in keywords[i:i + 100]]
        r = httpx.post(f"{BASE}/app_data/apple/app_searches/task_post", json=payload, auth=(self.login, self.password), timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        self.spent_usd += float(data.get("cost") or 0)
        for t in data.get("tasks", []):
            if t.get("status_code") in (20100, 20000):
                pending[t["id"]] = (t.get("data") or {}).get("tag") or (t.get("data") or {}).get("keyword")
                self.tasks += 1
    deadline = _t.time() + timeout_s
    while pending and _t.time() < deadline:
        _t.sleep(poll_seconds)
        ready = _get(self, "/app_data/apple/app_searches/tasks_ready")
        ids = [x["id"] for t in ready.get("tasks", []) for x in (t.get("result") or []) if x.get("id") in pending]
        for tid in ids:
            res = _get(self, f"/app_data/apple/app_searches/task_get/advanced/{tid}")
            self.spent_usd += float(res.get("cost") or 0)
            kw = pending.pop(tid)
            items = []
            for t in res.get("tasks", []):
                for rr in t.get("result") or []:
                    items.extend(rr.get("items") or [])
            yield kw, items
    for _tid, kw in pending.items():
        yield kw, []


DataForSEOClient.app_searches = app_searches


def summarize_trend(keyword: str, pts: list[tuple], ref: list[tuple], reference: str) -> dict:
    vals = [v for _, v in pts]
    refvals = [v for _, v in ref]
    last12 = vals[-52:] if len(vals) >= 52 else vals  # weekly points over 5y; daily/monthly granularity handled by ratio below
    n = len(last12)
    slope = 0.0
    if n >= 4:
        xs = list(range(n))
        mx, my = sum(xs) / n, sum(last12) / n
        den = sum((x - mx) ** 2 for x in xs) or 1
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, last12)) / den * n  # change over window, in index points
        slope = slope / (my or 1)  # relative to mean level
    rel = (sum(vals[-12:]) / max(1, len(vals[-12:]))) / ((sum(refvals[-12:]) / max(1, len(refvals[-12:]))) or 1) if refvals else None
    return {"keyword": keyword, "reference": reference, "points": pts, "rel_to_reference": rel, "slope_12m": round(slope, 4)}


def play_reviews(self: DataForSEOClient, packages: list[str], depth: int = 150, sort_by: str = "newest",
                 location_code: int = US, language: str = EN, poll_seconds: float = 15.0, timeout_s: float = 2700.0):
    """Yield (package, items) of Google Play reviews. Standard queue only (no live mode): up to 45 min.
    $0.00075 per 150 reviews. depth is billed in blocks of 150."""
    import time as _t
    pending: dict[str, str] = {}
    for i in range(0, len(packages), 100):
        payload = [{"app_id": pkg, "location_code": location_code, "language_code": language, "depth": depth,
                    "sort_by": sort_by, "tag": pkg} for pkg in packages[i:i + 100]]
        r = httpx.post(f"{BASE}/app_data/google/app_reviews/task_post", json=payload, auth=(self.login, self.password), timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        self.spent_usd += float(data.get("cost") or 0)
        for t in data.get("tasks", []):
            if t.get("status_code") in (20100, 20000):
                pending[t["id"]] = (t.get("data") or {}).get("tag")
                self.tasks += 1
    deadline = _t.time() + timeout_s
    while pending and _t.time() < deadline:
        _t.sleep(poll_seconds)
        ready = _get(self, "/app_data/google/app_reviews/tasks_ready")
        for tid in [x["id"] for t in ready.get("tasks", []) for x in (t.get("result") or []) if x.get("id") in pending]:
            res = _get(self, f"/app_data/google/app_reviews/task_get/advanced/{tid}")
            self.spent_usd += float(res.get("cost") or 0)
            pkg = pending.pop(tid)
            yield pkg, [it for t in res.get("tasks", []) for rr in (t.get("result") or []) for it in (rr.get("items") or [])]
    for _tid, pkg in pending.items():
        yield pkg, []


DataForSEOClient.play_reviews = play_reviews
