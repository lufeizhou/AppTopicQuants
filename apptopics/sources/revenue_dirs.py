"""Verified-revenue directories used as calibration anchors.

TrustMRR (https://trustmrr.com/api/v1, bearer key, 20 req/min): revenue verified via RevenueCat/Stripe.
MobileMRR (MCP over HTTP, no key): revenue verified via RevenueCat/Superwall, includes App Store URLs.
Both report worldwide, all-platform revenue. Results are cached under data/cache/."""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

from ..config import settings

log = logging.getLogger("apptopics.revdirs")
CACHE = settings.db_path.parent / "cache"


def trustmrr_mobile(refresh: bool = False, page_size: int = 50) -> list[dict]:
    """All listings in TrustMRR's mobile-apps category (paged, resumable, cached)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    final, part = CACHE / "trustmrr_mobile.json", CACHE / "trustmrr_mobile.partial.jsonl"
    if final.exists() and not refresh:
        return json.loads(final.read_text())
    key = os.environ.get("TRUSTMRR_API_KEY") or getattr(settings, "trustmrr_api_key", "")
    if not key:
        raise RuntimeError("TRUSTMRR_API_KEY not set")
    have = [json.loads(l) for l in part.read_text().splitlines()] if part.exists() and not refresh else []
    page = len(have) // page_size + 1
    rows = have
    with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {key}"}) as c:
        while True:
            r = c.get("https://trustmrr.com/api/v1/startups", params={"category": "mobile-apps", "limit": page_size, "page": page})
            if r.status_code == 429:
                log.info("trustmrr rate-limited, waiting 30s"); time.sleep(30); continue
            r.raise_for_status()
            d = r.json()
            with part.open("a") as f:
                for x in d["data"]:
                    f.write(json.dumps(x) + "\n")
            rows += d["data"]
            log.info("trustmrr page %d: %d listings (total %s)", page, len(rows), d["meta"].get("total"))
            if not d["meta"].get("hasMore") or not d["data"]:
                break
            page += 1
            time.sleep(3.5)
    final.write_text(json.dumps(rows))
    part.unlink(missing_ok=True)
    return rows
