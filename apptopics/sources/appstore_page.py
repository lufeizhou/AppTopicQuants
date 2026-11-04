"""Public App Store product page: the 'In-App Purchases' list (name + price), which the iTunes API does not return.

Honest client (identifies itself), shared machine-wide rate limit, one page per app. Prices are the US storefront's."""
from __future__ import annotations

import html as _html
import re

import httpx

from .base import RateLimiter

UA = "apptopics/0.1 (app market research; contact via App Store developer page)"
_limiter = RateLimiter(30, shared="apps_apple_page")
_PAIR = re.compile(r'<div class="text-pair[^"]*">\s*<span>([^<]{1,120})</span>\s*<span>\s*\$([\d,]+(?:\.\d{1,2})?)\s*</span>', re.S)


def parse_iap(page: str) -> list[dict]:
    """[{position, item, price_usd}] in page order (Apple lists the top ~10 purchases)."""
    i = page.find("In-App Purchase")
    if i < 0:
        return []
    seg = page[i:i + 40000]
    out = []
    for n, m in enumerate(_PAIR.finditer(seg), 1):
        out.append({"position": n, "item": _html.unescape(m.group(1)).strip(), "price_usd": float(m.group(2).replace(",", ""))})
    return out


def period_of(item: str, price: float) -> str:
    """week | month | year | lifetime | other, from the purchase name (falls back to 'other')."""
    s = item.lower()
    if re.search(r"week|weekly|7[- ]?day", s):
        return "week"
    if re.search(r"year|annual|annually|12[- ]?month|yearly", s):
        return "year"
    if re.search(r"lifetime|forever|one[- ]time|permanent|unlock all", s):
        return "lifetime"
    if re.search(r"month|monthly|30[- ]?day", s):
        return "month"
    if re.search(r"quarter|3[- ]?month|6[- ]?month|semi", s):
        return "other"
    return "other"


def fetch_iap(app_id: str, country: str = "us", client: httpx.Client | None = None) -> list[dict] | None:
    """None when the page could not be read (so callers don't cache a failure as 'no purchases')."""
    _limiter.wait()
    c = client or httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)
    try:
        r = c.get(f"https://apps.apple.com/{country}/app/id{app_id}")
        if r.status_code != 200:
            return None
        return parse_iap(r.text)
    except httpx.HTTPError:
        return None
    finally:
        if client is None:
            c.close()
