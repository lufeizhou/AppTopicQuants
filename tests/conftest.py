import datetime as dt

import pytest

from apptopics import db


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


@pytest.fixture
def today():
    return dt.date(2026, 11, 1)


def seed(con, d):
    """A tiny universe: a brand term and two generic terms."""
    db.upsert(con, "keywords", [
        {"store": "us", "keyword": "instagram", "genre": "6005", "popularity": 100, "rank_in_store": 1, "snapshot_date": d},
        {"store": "us", "keyword": "hiit timer", "genre": "6013", "popularity": 40, "rank_in_store": 2, "snapshot_date": d},
        {"store": "us", "keyword": "snack tracker", "genre": "6013", "popularity": 9, "rank_in_store": 3, "snapshot_date": d},
    ])
    sr = []
    for kw, apps in {
        "instagram": [("1", "Instagram", "Meta", 30_000_000), ("2", "Threads", "Meta", 2_000_000)],
        "hiit timer": [("10", "Interval Timer HIIT Timer", "Float", 87_000), ("11", "Timer Plus", "ArGames", 61_000), ("12", "Tabata", "X", 19_000)],
        "snack tracker": [("20", "Snack Log", "A", 120), ("21", "Food Diary", "B", 80)],
    }.items():
        for i, (aid, name, dev, rc) in enumerate(apps, 1):
            sr.append({"store": "us", "platform": "iphone", "keyword": kw, "rank": i, "app_id": aid, "name": name, "developer": dev,
                       "genre": "g", "rating_count": rc, "avg_rating": 4.5, "subtitle": "", "total_results": 50, "snapshot_date": d})
    db.upsert(con, "search_results", sr)
    db.upsert(con, "chart_entries", [
        {"store": "us", "chart": "grossing", "genre_id": "all", "rank": 3, "app_id": "1", "name": "Instagram", "developer": "Meta", "price": 0, "release_date": None, "snapshot_date": d},
        {"store": "us", "chart": "grossing", "genre_id": "6013", "rank": 20, "app_id": "10", "name": "Interval Timer", "developer": "Float", "price": 0, "release_date": None, "snapshot_date": d},
    ])
