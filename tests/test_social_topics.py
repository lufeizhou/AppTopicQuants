"""Offline checks for topic-level TikTok/YouTube/Meta ads collection (fake ScrapeCreators client, no database writes)."""
import json
from unittest import mock

import apptopics.pipeline.social_topics as st
from apptopics.sources.scrapecreators import CreditCap


class Fake:
    def __init__(self, max_credits=100):
        self.max_credits, self.used, self.remaining, self.available = max_credits, 0, 500, True

    def _hit(self):
        if self.used >= self.max_credits:
            raise CreditCap("cap")
        self.used += 1

    def tiktok_search(self, q):
        self._hit()
        return {"search_item_list": [{"aweme_id": "1", "desc": "protein hacks", "url": "https://t/1", "create_time": 1790000000,
                                      "statistics": {"play_count": 9000, "digg_count": 10, "comment_count": 3}},
                                     {"aweme_id": "2", "desc": "paid promo", "is_ad": True, "statistics": {"play_count": 1}},
                                     {"aweme_id": "3", "desc": "more protein", "url": "https://t/3", "statistics": {"play_count": 1000}}]}

    def youtube_search(self, q):
        self._hit()
        return {"videos": [{"id": "v1", "url": "https://y/v1", "title": "Protein 101", "viewCountInt": 400, "publishedTime": "2026-05-01T00:00:00Z"}]}

    def fb_companies(self, q):
        self._hit()
        return {"searchResults": [{"name": "Shotsy App", "page_id": "p1"}]}

    def fb_company_ads(self, page_id):
        self._hit()
        return {"results": [{"ad_archive_id": "a1", "is_active": True, "start_date_string": "2026-08-01T00:00:00.000Z",
                             "publisher_platform": ["FACEBOOK"], "snapshot": {"title": "Track", "body": {"text": "x"},
                                                                              "link_url": "https://apps.apple.com/app/id6499510249"}}]}

    def cost_usd(self):
        return round(self.used * 0.00188, 4)


class FakeCon:
    def __init__(self):
        self.labels = {"t1": "Protein tracker"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        return self

    def fetchall(self):
        if "FROM topics" in self.sql:
            return list(self.labels.items())
        if "FROM topic_keywords" in self.sql:
            return [("t1", "protein tracker")]
        return []

    def fetchone(self):
        return (None,)


def _run(fake, **kw):
    saved = {}

    def save(fn):
        class C:
            def execute(self, *a):
                return None
        rows = []
        with mock.patch.object(st, "upsert", lambda c, t, r: rows.append((t, r))), \
             mock.patch("apptopics.pipeline.social_voice.upsert", lambda c, t, r: rows.append((t, r))):
            fn(C())
        saved["rows"] = rows
    with mock.patch.object(st, "ScrapeCreators", lambda max_credits: fake, create=True), \
         mock.patch("apptopics.sources.scrapecreators.ScrapeCreators", lambda max_credits: fake), \
         mock.patch.object(st, "connect_read_live", lambda: FakeCon()), \
         mock.patch.object(st, "_top_apps", lambda con, store, kws, n: [{"app_id": "6499510249", "name": "Shotsy: GLP-1 Tracker", "developer": "Shotsy"}]), \
         mock.patch.object(st, "_save_rows", save), \
         mock.patch("apptopics.pipeline.social_voice._save_rows", lambda fn: None), \
         mock.patch("apptopics.pipeline.discover.write_retry", lambda fn, **k: None), \
         mock.patch("apptopics.budget.check", lambda con, api, est: (True, "ok")), \
         mock.patch("apptopics.budget.record", lambda *a, **k: None):
        return st.collect_topics(["t1"], **kw), saved


def test_summary_and_credits():
    fake = Fake()
    r, saved = _run(fake, ads_per_topic=1)
    t = r["topics"][0]
    assert t["tiktok_videos"] == 2 and t["tiktok_views"] == 10000 and t["tiktok_median_views"] == 5000   # ad item skipped
    assert t["youtube_videos"] == 1 and t["youtube_views"] == 400
    assert t["apps_checked"] == 1 and t["apps_with_ads"] == 1 and t["apps_verified_ads"] == 1 and t["max_active_ads"] == 1
    assert t["max_oldest_active_days"] > 60
    assert json.loads(t["ad_apps"])[0]["name"].startswith("Shotsy")
    assert t["credits"] == 4 and r["credits_used"] == 4 and abs(r["usd"] - 4 * 0.00188) < 1e-3   # 2 searches + company + ads
    posts = dict(saved["rows"])["social_posts"]
    assert {p["id"] for p in posts} == {"tt_1", "tt_3", "yt_v1"} and all(p["topic"] == "t1" and p["aspect"] == "_topic" for p in posts)
    assert all("user" not in p and "author" not in p for p in posts)


def test_credit_cap_stops_cleanly():
    fake = Fake(max_credits=1)
    r, _ = _run(fake, ads=False)
    assert r["credits_used"] == 1 and r["topics"][0]["tiktok_videos"] == 2 and r["topics"][0]["youtube_videos"] is None
