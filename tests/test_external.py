import datetime as dt
import json

import httpx
import pytest

from apptopics import db
from apptopics.pipeline.anchor_fit import import_anchors
from apptopics.sources.dataforseo import summarize_trend
from apptopics.sources.itunes import ITunesClient

D = dt.date(2026, 11, 1)


def test_summarize_trend_slope_and_reference():
    pts = [(f"2026-{m:02d}-01", v) for m, v in enumerate([10, 10, 10, 10, 20, 20, 20, 20, 30, 30, 30, 30], 1)]
    ref = [(p[0], 50) for p in pts]
    t = summarize_trend("hiit timer", pts, ref, "calendar app")
    assert t["slope_12m"] > 0.5                      # rising series
    assert t["rel_to_reference"] == pytest.approx(20 / 50)
    flat = summarize_trend("x", [(p[0], 20) for p in pts], ref, "calendar app")
    assert flat["slope_12m"] == 0


def test_reviews_feed_parsing():
    feed = {"feed": {"entry": [
        {"id": {"label": "r1"}, "im:rating": {"label": "1"}, "title": {"label": "Crashes"}, "content": {"label": "crashes on start"},
         "im:version": {"label": "2.0"}, "updated": {"label": "2026-10-30T10:00:00-07:00"}},
        {"id": {"label": "r2"}, "im:rating": {"label": "5"}, "title": {"label": "Great"}, "content": {"label": "love it"},
         "im:version": {"label": "2.0"}, "updated": {"label": "2026-10-29T10:00:00-07:00"}},
    ]}}
    c = ITunesClient()
    c.rss_limiter.interval = 0
    c._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=feed)))
    revs = c.reviews("123", pages=3)
    assert [r["rating"] for r in revs] == [1, 5] and revs[0]["review_id"] == "r1"


def test_topic_index_from_members(con):
    import math

    from apptopics.pipeline.topics import build_topic_index
    db.upsert(con, "topics", [{"topic_id": "interval-timers", "label": "Interval timers", "description": "", "created_at": dt.datetime.now()}])
    db.upsert(con, "topics", [{"topic_id": "tiktok", "label": "TikTok", "description": "", "created_at": dt.datetime.now()}])
    db.upsert(con, "topic_keywords", [{"topic_id": "interval-timers", "store": "us", "keyword": k, "weight": 1.0}
                                      for k in ("hiit timer", "tabata timer", "boxing timer")]
              + [{"topic_id": "tiktok", "store": "us", "keyword": k, "weight": 1.0} for k in ("tiktok", "tik tok")])
    now = dt.datetime.now()
    def fv(k, metric, v, meta=None):
        return {"entity_type": "keyword", "entity_id": k, "metric": metric, "value": v, "meta": json.dumps(meta) if meta else None,
                "snapshot_date": D, "computed_at": now}
    rows = []
    for k, s, g, pop, hidden in [("hiit timer", 52.0, "A", 40, None), ("tabata timer", 30.0, "C", 20, None), ("boxing timer", 60.0, "A", 30, "games"),
                                 ("tiktok", 80.0, "A", 90, "label:brand"), ("tik tok", 70.0, "A", 70, "label:brand")]:
        rows += [fv(k, "opp_score", s, {"grade": g, "action": "Build now" if g == "A" else "Watch", "reason": "", "parts": {"score": s}}),
                 fv(k, "composite", 0.0, {"excluded": bool(hidden), "excluded_reason": hidden or ""}), fv(k, "demand", pop)]
    db.upsert(con, "factor_values", rows)
    assert build_topic_index(con, "us", D) == 2                        # the visible topic + the hidden-only one
    m = json.loads(con.execute("SELECT meta FROM factor_values WHERE entity_type='topic' AND entity_id='interval-timers' AND metric='opp_score'").fetchone()[0])
    v = con.execute("SELECT value FROM factor_values WHERE entity_type='topic' AND entity_id='interval-timers' AND metric='opp_score'").fetchone()[0]
    assert m["best"] == "hiit timer" and m["n_members"] == 2           # hard-hidden member (games) is dropped from the score
    assert m["n_hidden"] == 1 and m["hidden_members"][0]["kind"] == "games" and not m["hidden"]
    assert con.execute("SELECT count(*) FROM factor_values WHERE entity_type='topic' AND entity_id='interval-timers' AND metric='composite'").fetchone()[0] == 0
    hm = json.loads(con.execute("SELECT meta FROM factor_values WHERE entity_type='topic' AND entity_id='tiktok' AND metric='opp_score'").fetchone()[0])
    assert hm["hidden"] and hm["action"] == "Avoid" and hm["best"] == "tiktok" and hm["hidden_kinds"] == "brand 2"
    cm = json.loads(con.execute("SELECT meta FROM factor_values WHERE entity_type='topic' AND entity_id='tiktok' AND metric='composite'").fetchone()[0])
    assert cm["excluded"] and "brand" in cm["excluded_reason"]
    assert 0.9 < m["traffic_adj"] < 1.0                                  # ~74 searches/day -> small penalty
    assert abs(v - (0.75 * 52 + 0.25 * 41 + 2 * math.log2(3)) * m["traffic_adj"]) < 0.1
    assert m["action"] == "Build now"


def test_anchor_import(con, tmp_path):
    p = tmp_path / "anchors.csv"
    p.write_text("app_id,monthly_revenue_usd,source\n" + "\n".join(f"a{r},{1e6 / r},manual" for r in (1, 10, 100)) + "\n")
    assert import_anchors(con, p) == 3
    assert con.execute("SELECT count(*) FROM calibration_anchors WHERE source='manual'").fetchone()[0] == 3
