"""Offline checks for TikTok/YouTube parsing and Meta ad matching (fake ScrapeCreators responses)."""
from unittest import mock

import apptopics.pipeline.social_voice as sv


class Fake:
    used, remaining = 0, 100

    def tiktok_search(self, q):
        return {"search_item_list": [{"aweme_id": "1", "desc": "glp1 protein meals", "url": "https://t/1", "create_time": 1790000000,
                                      "statistics": {"play_count": 5000, "digg_count": 10, "comment_count": 3}},
                                     {"aweme_id": "2", "desc": "ad", "is_ad": True, "statistics": {}}]}

    def tiktok_comments(self, url):
        return {"comments": [{"cid": "c1", "text": "need easy protein", "digg_count": 4, "user": {"nickname": "x"}}]}

    def youtube_search(self, q):
        return {"videos": [{"id": "v1", "url": "https://y/v1", "title": "GLP-1 meals", "viewCountInt": 900, "publishedTime": "2026-05-01T00:00:00Z"}]}

    def youtube_comments(self, url):
        return {"comments": [{"id": "k1", "content": "great tips", "engagement": {"likes": 2}, "author": {"name": "@y"}}]}

    def fb_companies(self, q):
        return {"searchResults": [{"name": "Shotsy App", "page_id": "p1"}, {"name": "Shotsyandco", "page_id": "p2"}]}

    def fb_company_ads(self, page_id):
        return {"results": [{"ad_archive_id": "a1", "is_active": True, "start_date_string": "2026-09-01T00:00:00.000Z",
                             "publisher_platform": ["FACEBOOK"], "snapshot": {"title": "Track GLP-1", "body": {"text": "1M downloads"},
                                                                              "link_url": "https://app.appsflyer.com/id6499510249?x=1"}}]}


def test_video_and_ads():
    with mock.patch.object(sv, "write_retry", lambda f, **k: None):
        posts, comments, boxes = sv.collect_video(Fake(), "glp-1", [{"name": "Protein meals"}])
        assert {p["platform"] for p in posts.values()} == {"tiktok", "youtube"}
        assert "tt_2" not in posts                                   # ads skipped
        assert all("user" not in c and "author" not in c for c in comments.values())   # no names stored
        assert boxes[0]["tiktok"]["views"] == 5000 and boxes[0]["youtube"]["comments"] == 1
        ads = sv.collect_ads(Fake(), [{"app_id": "6499510249", "name": "Shotsy: GLP-1 Tracker", "developer": "Shotsy"},
                                      {"app_id": "9", "name": "Nothing Alike", "developer": "Q"}])
        assert ads[0]["verified"] and ads[0]["active_ads"] == 1 and ads[0]["page"] == "Shotsy App"
        assert ads[1]["page"] is None
