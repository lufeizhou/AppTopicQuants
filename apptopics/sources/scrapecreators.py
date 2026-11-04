"""ScrapeCreators (third-party) Reddit data. 1 credit per request; the client refuses to go past `max_credits`.

Chosen by the user over the official Reddit API (pending approval). Internal research only: no usernames are kept."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

BASE = "https://api.scrapecreators.com"
USD_PER_CREDIT = 47 / 25000


def _key() -> str:
    return os.environ.get("SCRAPECREATORS_API_KEY") or os.environ.get("ISCRAPECREATORS_API_KEY") or ""


class CreditCap(RuntimeError):
    pass


@dataclass
class ScrapeCreators:
    max_credits: int = 100
    used: int = 0
    remaining: int | None = None
    key: str = field(default_factory=_key)
    _c: httpx.Client | None = None

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, **params) -> dict:
        if self.used >= self.max_credits:
            raise CreditCap(f"credit cap {self.max_credits} reached")
        if self._c is None:
            self._c = httpx.Client(timeout=60, headers={"x-api-key": self.key})
        r = self._c.get(BASE + path, params={k: v for k, v in params.items() if v is not None})
        self.used += 1                      # count every attempt: failed calls may still be charged
        if r.status_code in (401, 402, 403, 429):   # out of credits / key / rate: stop, keep what was collected
            raise CreditCap(f"ScrapeCreators HTTP {r.status_code}")
        r.raise_for_status()
        d = r.json()
        if d.get("credits_remaining") is not None:
            self.remaining = int(d["credits_remaining"])
        return d

    def search(self, query: str, filter: str = "posts", sort: str = "relevance", timeframe: str = "year", after: str | None = None) -> dict:
        return self._get("/v1/reddit/search", query=query, filter=filter, sort=sort, timeframe=timeframe, after=after, trim="true")

    def comments(self, post_url: str) -> dict:
        return self._get("/v1/reddit/post/comments", url=post_url, trim="true")

    def subreddit(self, name: str) -> dict:
        return self._get("/v1/reddit/subreddit/details", subreddit=name, cache_max_age="30d")

    # TikTok / YouTube (voice of users; no usernames are stored)
    def tiktok_search(self, query: str, date_posted: str = "last-6-months") -> dict:
        return self._get("/v1/tiktok/search/keyword", query=query, date_posted=date_posted, sort_by="relevance", region="US", trim="true")

    def tiktok_comments(self, video_url: str) -> dict:
        return self._get("/v1/tiktok/video/comments", url=video_url, trim="true")

    def youtube_search(self, query: str) -> dict:
        return self._get("/v1/youtube/search", query=query, type="videos", sortBy="relevance", region="US", uploadDate="this_year")

    def youtube_comments(self, video_url: str) -> dict:
        return self._get("/v1/youtube/video/comments", url=video_url, order="top")

    # Meta Ad Library (which competitors buy ads, for how long, with which message)
    def fb_companies(self, query: str) -> dict:
        return self._get("/v1/facebook/adLibrary/search/companies", query=query)

    def fb_company_ads(self, page_id: str, status: str = "ACTIVE") -> dict:
        return self._get("/v1/facebook/adLibrary/company/ads", pageId=page_id, status=status, country="US", trim="true")

    def cost_usd(self) -> float:
        return round(self.used * USD_PER_CREDIT, 4)
