import datetime as dt

import httpx
import pytest

from apptopics import db
from apptopics.metrics import metrics
from apptopics.sources.appleads import AppleAdsClient, rows_to_db, week_start
from apptopics.sources.dataforseo import DataForSEOClient


def test_week_start_is_sunday():
    assert week_start(dt.date(2026, 11, 1)) == dt.date(2026, 11, 1)   # Sunday
    assert week_start(dt.date(2026, 11, 4)) == dt.date(2026, 11, 1)


def test_appleads_rows_and_trending(con):
    rows = [{"countryOrRegion": "US", "genre": "HEALTH_FITNESS", "searchTerm": "HIIT Timer", "week": "2026-10-25",
             "rankInGenre": 12, "searchPopularityInGenre": 70, "searchPopularity1to100": 45, "searchPopularity1to5": 3}]
    out = rows_to_db(rows, "US")
    assert out[0]["term"] == "hiit timer" and out[0]["week"] == dt.date(2026, 10, 25) and out[0]["country"] == "us"
    weeks = [dt.date(2026, 9, 27) + dt.timedelta(days=7 * i) for i in range(5)]
    data = []
    for i, w in enumerate(weeks):
        data.append({"country": "us", "genre": "g", "term": "hiit timer", "week": w, "rank_in_genre": 1, "pop_in_genre": 1, "pop_1to100": 40 + (10 if i == 4 else 0), "pop_1to5": 3})
        data.append({"country": "us", "genre": "g", "term": "instagram", "week": w, "rank_in_genre": 1, "pop_in_genre": 1, "pop_1to100": 100, "pop_1to5": 5})
    data.append({"country": "us", "genre": "g", "term": "brand new thing", "week": weeks[-1], "rank_in_genre": 9, "pop_in_genre": 9, "pop_1to100": 62, "pop_1to5": 2})
    # drops in and out near the genre cut-off: seen at 56 six weeks ago, 58 now -> +2, not a spike
    data.append({"country": "us", "genre": "g", "term": "counter", "week": weeks[0], "rank_in_genre": 500, "pop_in_genre": 1, "pop_1to100": 56, "pop_1to5": 2})
    data.append({"country": "us", "genre": "g", "term": "counter", "week": weeks[-1], "rank_in_genre": 480, "pop_in_genre": 1, "pop_1to100": 58, "pop_1to5": 2})
    db.upsert(con, "popularity_weekly", data)
    rows = {r.entity_id: r for r in metrics["trending"]().compute(con, "us", dt.date(2026, 11, 1))}
    assert rows["hiit timer"].value == pytest.approx(10.0) and rows["instagram"].value == 0.0
    # never seen before: compared with the genre cut-off that week (lowest listed popularity = 50)
    assert rows["brand new thing"].meta["new_to_list"] and rows["brand new thing"].value == 12.0
    assert rows["counter"].value == 2.0
    from apptopics.pipeline.collect import keywords_from_popularity
    assert keywords_from_popularity(con, "us") == 4


def test_appleads_client_secret_shape(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    c = AppleAdsClient(client_id="SEARCHADS.x", team_id="SEARCHADS.t", key_id="kid1", private_key_path=str(pem))
    import jwt
    tok = c.client_secret(now=1_700_000_000)
    hdr, body = jwt.get_unverified_header(tok), jwt.decode(tok, options={"verify_signature": False}, audience="https://appleid.apple.com")
    assert hdr["alg"] == "ES256" and hdr["kid"] == "kid1"
    assert body["sub"] == "SEARCHADS.x" and body["iss"] == "SEARCHADS.t" and body["exp"] - body["iat"] < 180 * 86400


def test_dataforseo_app_searches_flow(monkeypatch):
    calls = []

    def fake_post(url, json=None, auth=None, timeout=None):
        calls.append(("post", url))
        return httpx.Response(200, json={"cost": 0.0048, "tasks": [{"id": f"t{i}", "status_code": 20100, "data": {"tag": k["keyword"]}} for i, k in enumerate(json)]},
                              request=httpx.Request("POST", url))

    def fake_get(url, auth=None, timeout=None):
        calls.append(("get", url))
        if url.endswith("tasks_ready"):
            return httpx.Response(200, json={"tasks": [{"result": [{"id": "t0"}, {"id": "t1"}]}]}, request=httpx.Request("GET", url))
        tid = url.rsplit("/", 1)[-1]
        item = {"type": "app_item", "app_id": "111" if tid == "t0" else "222", "title": "A", "developer": "D", "rank_absolute": 1,
                "rating": {"value": 4.7, "votes_count": 12345}, "price": 0, "is_free": True}
        return httpx.Response(200, json={"cost": 0, "tasks": [{"result": [{"items": [item]}]}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    c = DataForSEOClient(login="l", password="p")
    out = dict(c.app_searches(["hiit timer", "tabata"], poll_seconds=0))
    assert out["hiit timer"][0]["app_id"] == "111" and out["tabata"][0]["app_id"] == "222"
    assert c.tasks == 2 and c.spent_usd == pytest.approx(0.0048)
