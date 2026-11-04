"""Free Apple endpoints: iTunes Lookup (metadata, price, ratings) and the legacy
RSS charts (top grossing / free / paid per genre, max 100 entries)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import httpx

from ..registry import sources
from .base import RateLimiter

CHARTS = {
    "grossing": "topgrossingapplications",
    "free": "topfreeapplications",
    "paid": "toppaidapplications",
}


@sources.register("itunes")
@dataclass
class ITunesClient:
    timeout: float = 30.0
    # Apple throttles the lookup/search API at roughly 20 calls/min.
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(18, shared="itunes"))
    rss_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(90))
    _client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, headers={"User-Agent": "apptopics/0.1"})
        return self._client

    def lookup(self, app_ids: list[str], country: str = "us") -> list[dict]:
        """Lookup up to ~200 ids per call."""
        out: list[dict] = []
        for i in range(0, len(app_ids), 150):
            chunk = app_ids[i:i + 150]
            self.limiter.wait()
            r = self._http().get("https://itunes.apple.com/lookup",
                                 params={"id": ",".join(chunk), "country": country, "entity": "software"})
            r.raise_for_status()
            out.extend(r.json().get("results", []))
        return out

    def hints(self, term: str, storefront: str = "143441-1,29") -> list[str]:
        """App Store search autocomplete (public, keyless; storefront 143441 = US). Ordered as Apple shows them."""
        import re as _re
        self.limiter.wait()
        r = self._http().get("https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints",
                             params={"clientApplication": "Software", "term": term},
                             headers={"X-Apple-Store-Front": storefront})
        r.raise_for_status()
        terms = _re.findall(r"<key>term</key>\s*<string>([^<]*)</string>", r.text)
        import html as _h
        return [_h.unescape(t).strip().lower() for t in terms]

    def chart(self, chart: str, genre_id: str | None, country: str = "us", limit: int = 100) -> list[dict]:
        feed = CHARTS[chart]
        path = f"https://itunes.apple.com/{country}/rss/{feed}/limit={limit}/"
        if genre_id:
            path += f"genre={genre_id}/"
        path += "json"
        self.rss_limiter.wait()
        r = self._http().get(path)
        r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
        rows = []
        for i, e in enumerate(entries, start=1):
            price = e.get("im:price", {}).get("attributes", {}).get("amount")
            rel = e.get("im:releaseDate", {}).get("label")
            rows.append({
                "rank": i,
                "app_id": e.get("id", {}).get("attributes", {}).get("im:id"),
                "name": e.get("im:name", {}).get("label"),
                "developer": e.get("im:artist", {}).get("label"),
                "price": float(price) if price not in (None, "") else None,
                "release_date": dt.date.fromisoformat(rel[:10]) if rel else None,
            })
        return rows


    def search(self, term: str, country: str = "us", limit: int = 200) -> list[dict]:
        """iTunes Search API: up to 200 software results with full metadata (relevance order)."""
        self.limiter.wait()
        r = self._http().get("https://itunes.apple.com/search", params={"term": term, "country": country, "entity": "software", "limit": limit})
        r.raise_for_status()
        return r.json().get("results", [])

    def reviews(self, app_id: str, country: str = "us", pages: int = 2) -> list[dict]:
        """Most recent customer reviews (50 per page, max 10 pages)."""
        out = []
        for page in range(1, pages + 1):
            self.rss_limiter.wait()
            r = self._http().get(f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json")
            if r.status_code != 200:
                break
            entries = r.json().get("feed", {}).get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            for e in entries:
                if "im:rating" not in e:
                    continue
                out.append({"review_id": e.get("id", {}).get("label"), "rating": int(e["im:rating"]["label"]),
                            "title": e.get("title", {}).get("label"), "body": (e.get("content", {}).get("label") or "")[:2000],
                            "version": e.get("im:version", {}).get("label"), "updated": e.get("updated", {}).get("label")})
            if len(entries) < 50:
                break
        return out


def lookup_to_row(r: dict, store: str, snapshot_date: dt.date) -> dict:
    def ts(s):
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None) if s else None
    return {
        "store": store, "app_id": str(r.get("trackId")), "name": r.get("trackName"),
        "developer": r.get("sellerName") or r.get("artistName"), "bundle_id": r.get("bundleId"),
        "primary_genre_id": str(r.get("primaryGenreId")) if r.get("primaryGenreId") else None,
        "genre_ids": list(r.get("genreIds") or []), "price": r.get("price"), "currency": r.get("currency"),
        "release_date": ts(r.get("releaseDate")), "current_version_date": ts(r.get("currentVersionReleaseDate")),
        "rating_count": r.get("userRatingCount"), "avg_rating": r.get("averageUserRating"),
        "rating_count_current": r.get("userRatingCountForCurrentVersion"), "min_os": r.get("minimumOsVersion"),
        "file_size_bytes": int(r["fileSizeBytes"]) if r.get("fileSizeBytes") else None,
        "description": (r.get("description") or "")[:4000], "snapshot_date": snapshot_date,
    }
