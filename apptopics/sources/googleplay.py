"""Google Play via play-store-scraper-ng (module name google_play_scraper)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..registry import sources
from .base import RateLimiter


@sources.register("googleplay")
@dataclass
class PlayClient:
    country: str = "us"
    lang: str = "en"
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(40))
    _client: object = None

    def _gp(self):
        if self._client is None:
            from google_play_scraper import GooglePlayClient
            self._client = GooglePlayClient()
        return self._client

    def search(self, term: str, num: int = 30) -> list[dict]:
        self.limiter.wait()
        res = self._gp().search(term, num=num, lang=self.lang, country=self.country)
        return [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in res]

    def suggest(self, term: str) -> list[str]:
        """Google Play search autocomplete (free, unofficial batchexecute endpoint 'IJ4APc'; may change without notice).
        Returns up to ~5 phrases in the order Play shows them."""
        import json as _json

        import httpx
        self.limiter.wait()
        freq = _json.dumps([[["IJ4APc", _json.dumps([[None, [term], [10], [2], 4]]), None, "generic"]]])
        r = httpx.post("https://play.google.com/_/PlayStoreUi/data/batchexecute",
                       params={"rpcids": "IJ4APc", "hl": self.lang, "gl": self.country}, data={"f.req": freq},
                       headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=20)
        r.raise_for_status()
        for line in r.text.splitlines():
            if line.startswith('[["wrb.fr"'):
                inner = _json.loads(line)[0][2]
                if not inner:
                    return []
                data = _json.loads(inner)
                try:
                    return [str(e[0]).strip().lower() for e in data[0][0] if e and e[0]]
                except (IndexError, TypeError):
                    return []
        return []

    def chart(self, collection: str = "GROSSING", category: str = "APPLICATION", num: int = 100) -> list[dict]:
        from google_play_scraper import Category, Collection
        self.limiter.wait()
        res = self._gp().list(collection=getattr(Collection, collection), category=getattr(Category, category), num=num,
                              lang=self.lang, country=self.country)
        return [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in res]

    @staticmethod
    def categories() -> list[str]:
        from google_play_scraper import Category
        return [c.name for c in Category if not c.name.startswith("GAME_") and c.name not in ("FAMILY",)]

    def details(self, package: str) -> dict | None:
        from google_play_scraper import AppNotFound
        self.limiter.wait()
        try:
            d = self._gp().app(package, lang=self.lang, country=self.country)
        except AppNotFound:
            return None
        return d.model_dump() if hasattr(d, "model_dump") else dict(d)


def details_to_row(d: dict, country: str, snapshot_date: dt.date) -> dict:
    upd = d.get("updated")
    if isinstance(upd, (int, float)):
        upd = dt.datetime.fromtimestamp(upd)
    elif isinstance(upd, str):
        try:
            upd = dt.datetime.fromisoformat(upd)
        except ValueError:
            upd = None
    return {
        "country": country, "package": d.get("app_id"), "title": d.get("title"), "developer": d.get("developer"),
        "genre": d.get("genre"), "genre_id": d.get("genre_id"), "score": d.get("score"),
        "ratings": d.get("ratings"), "reviews": d.get("reviews"), "installs_text": d.get("installs"),
        "min_installs": d.get("min_installs"), "max_installs": d.get("max_installs"),
        "price": d.get("price"), "free": d.get("free"), "offers_iap": d.get("offers_iap"),
        "released": d.get("released"), "updated": upd, "snapshot_date": snapshot_date,
    }
