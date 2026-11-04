"""Apple Ads Platform API: weekly App Store search-term popularity (the dataset behind
Astro's "most searched" list, ~7,500 terms per country per week, by genre).

Access: an Apple Ads account admin invites an API user; upload an EC P-256 public key under
Account Settings > API; Apple issues clientId, teamId, keyId. Client secret = ES256 JWT.
Env: APPLE_ADS_CLIENT_ID, APPLE_ADS_TEAM_ID, APPLE_ADS_KEY_ID, APPLE_ADS_PRIVATE_KEY_PATH,
optional APPLE_ADS_AD_ACCOUNT_ID (sent as X-AP-Context if set).
Endpoint: POST https://api.ads.apple.com/v1/insights/apps/search-term-popularity/query
"""
from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field

import httpx

from ..registry import sources

TOKEN_URL = "https://appleid.apple.com/auth/oauth2/token"
BASE = "https://api.ads.apple.com/v1"
FIELDS = ["rankInGenre", "searchPopularityInGenre", "searchPopularity1to100", "searchPopularity1to5"]


def week_start(d: dt.date) -> dt.date:
    """Sunday on or before d (Apple weeks are Sun-Sat, UTC)."""
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


@sources.register("appleads")
@dataclass
class AppleAdsClient:
    client_id: str = field(default_factory=lambda: os.environ.get("APPLE_ADS_CLIENT_ID", ""))
    team_id: str = field(default_factory=lambda: os.environ.get("APPLE_ADS_TEAM_ID", ""))
    key_id: str = field(default_factory=lambda: os.environ.get("APPLE_ADS_KEY_ID", ""))
    private_key_path: str = field(default_factory=lambda: os.environ.get("APPLE_ADS_PRIVATE_KEY_PATH", ""))
    ad_account_id: str = field(default_factory=lambda: os.environ.get("APPLE_ADS_AD_ACCOUNT_ID", ""))
    _token: tuple[str, float] | None = None

    @property
    def available(self) -> bool:
        return all([self.client_id, self.team_id, self.key_id, self.private_key_path]) and os.path.exists(self.private_key_path)

    def client_secret(self, now: float | None = None) -> str:
        import jwt
        now = now or time.time()
        with open(self.private_key_path) as f:
            key = f.read()
        return jwt.encode({"sub": self.client_id, "aud": "https://appleid.apple.com", "iat": int(now), "exp": int(now + 86400 * 150),
                           "iss": self.team_id}, key, algorithm="ES256", headers={"kid": self.key_id, "alg": "ES256"})

    def token(self) -> str:
        if self._token and self._token[1] - 60 > time.time():
            return self._token[0]
        r = httpx.post(TOKEN_URL, data={"grant_type": "client_credentials", "client_id": self.client_id,
                                        "client_secret": self.client_secret(), "scope": "searchadsorg"},
                       headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        self._token = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
        return self._token[0]

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        if self.ad_account_id:
            headers["X-AP-Context"] = f"adAccountId={self.ad_account_id}"
        wait = 1.0
        for _ in range(6):
            r = httpx.post(f"{BASE}{path}", json=body, headers=headers, timeout=120)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", wait)))
                wait = min(16.0, wait * 2)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("apple ads: rate limited repeatedly")

    def search_term_popularity(self, country: str = "US", start: dt.date | None = None, end: dt.date | None = None,
                               genre: str | None = None, granularity: str = "WEEKLY_SUN_SAT", page_size: int = 5000) -> list[dict]:
        """All rows for one time range (paginated)."""
        end = end or week_start(dt.date.today()) - dt.timedelta(days=1)   # last complete Sat
        start = start or week_start(end)
        filters = [{"field": "countryOrRegion", "operator": "EQUALS", "value": country.upper()}]
        if genre:
            filters.append({"field": "genre", "operator": "EQUALS", "value": genre})
        rows, offset = [], 0
        while True:
            body = {"fields": FIELDS, "filters": filters,
                    "timeRange": {"start": start.isoformat(), "end": end.isoformat(), "granularity": granularity},
                    "pagination": {"offset": offset, "pageSize": page_size}}
            data = self._post("/insights/apps/search-term-popularity/query", body)
            page = (data.get("result") or {}).get("rows") or []
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size


def rows_to_db(rows: list[dict], country: str) -> list[dict]:
    out = []
    for r in rows:
        wk = r.get("week") or r.get("month")
        out.append({"country": country.lower(), "genre": r.get("genre"), "term": (r.get("searchTerm") or "").strip().lower(),
                    "week": dt.date.fromisoformat(wk[:10]) if wk else None, "rank_in_genre": r.get("rankInGenre"),
                    "pop_in_genre": r.get("searchPopularityInGenre"), "pop_1to100": r.get("searchPopularity1to100"),
                    "pop_1to5": r.get("searchPopularity1to5")})
    return [o for o in out if o["term"] and o["week"]]
