import datetime as dt

import pytest

from apptopics import db
from apptopics.metrics.sector import band, estimate_app_revenue
from apptopics.pipeline.score import run_metrics

CFG = {"a": 1.5e6, "b": 1.1, "rpm_knots": [1, 10000], "usd_knots": [40, 400000], "usd_per_rating_month": 0.5}   # curve: $40 per rating/month
D = dt.date(2026, 11, 1)


def test_estimator_preference():
    now = dt.datetime(2026, 11, 1)
    old, ver = now - dt.timedelta(days=400), now - dt.timedelta(days=30)
    assert estimate_app_revenue(CFG, D, 1000, 100, old, ver, 10)[1] == "grossing"
    # observed day-over-day ratings win over everything but the charts
    est, src = estimate_app_revenue(CFG, D, 1000, 100, old, ver, None, observed=(70, 7))
    assert src == "observed" and est == pytest.approx(10 * 30.4 * 50 * 0.8)
    # per-version count that is a real reset (< 50% of lifetime) and >= 14 days old
    est, src = estimate_app_revenue(CFG, D, 1000, 100, old, ver, None)
    assert src == "velocity" and est == pytest.approx(100 / 30 * 30.4 * 50 * 0.8, rel=1e-3)
    # Apple reporting the lifetime count as the per-version count must NOT be treated as velocity
    assert estimate_app_revenue(CFG, D, 204_069, 204_069, old, now - dt.timedelta(days=14), None)[1] == "lifetime"
    assert estimate_app_revenue(CFG, D, 1000, 100, old, now - dt.timedelta(days=3), None)[1] == "lifetime"
    assert estimate_app_revenue(CFG, D, 1000, None, None, None, None) == (500.0, "ratings")


def test_band_indices():
    apps = list(range(100))
    assert band(apps, 0.05, 0.20) == list(range(5, 20))
    assert band(list(range(10)), 0.05, 0.20) == [0, 1]


def _sector(con, kw, ests, new_from=None):
    """Create a sector whose per-app 'ratings' fallback estimate equals ests[i] (usd_per_rating_month=0.5)."""
    rows, apps = [], []
    for i, e in enumerate(ests, 1):
        aid = f"{kw[:2]}{i}"
        rows.append({"store": "us", "platform": "iphone", "keyword": kw, "rank": i, "app_id": aid, "name": f"{kw} {i}", "developer": f"{kw}-dev{i}",
                     "genre": "g", "rating_count": int(e / 0.5), "avg_rating": 4.5, "subtitle": "", "total_results": 100, "snapshot_date": D})
        apps.append({"store": "us", "app_id": aid, "name": f"{kw} {i}", "developer": f"{kw}-dev{i}", "bundle_id": "b", "primary_genre_id": "6013", "genre_ids": [],
                     "price": 0.0, "currency": "USD", "release_date": dt.datetime(2026, 1, 1) if (new_from and i >= new_from) else dt.datetime(2015, 1, 1),
                     "current_version_date": None, "rating_count": int(e / 0.5), "avg_rating": 4.5, "rating_count_current": 0, "min_os": "17",
                     "file_size_bytes": 0, "description": "", "snapshot_date": D})
    db.upsert(con, "search_results", rows)
    db.upsert(con, "apps", apps)


def test_sector_metrics_prefer_broad_sectors(con, monkeypatch):
    # Make 'lifetime' estimates use months since 2015 -> override cfg to pure ratings fallback by removing dates? Simpler:
    # patch estimator to the ratings fallback so numbers are exact.
    import apptopics.metrics.sector as sec
    monkeypatch.setattr(sec, "estimate_app_revenue", lambda cfg, asof, rc, rcc, rel, ver, best, observed=None: (float(rc or 0) * 0.5, "ratings"))
    sec._cache.clear()
    # 'ai chat': one app with $2M, the rest $200 each  -> concentrated, nobody in band earns the target
    _sector(con, "ai chat", [2_000_000] + [200] * 39)
    # 'habit tracker': gentle distribution: 40 apps from $40k down to $1k -> broad
    _sector(con, "habit tracker", [40_000 - 1_000 * i for i in range(40)], new_from=3)
    counts = run_metrics(con, "us", D, only=["breadth", "attainable", "concentration", "entrant_share", "indie_share", "monetization"])
    assert counts == {"breadth": 2, "attainable": 2, "concentration": 2, "entrant_share": 2, "indie_share": 2, "monetization": 2}
    f = {(m, e): v for e, m, v in con.execute("SELECT entity_id, metric, value FROM factor_values").fetchall()}
    assert f[("concentration", "ai chat")] > 0.95 > f[("concentration", "habit tracker")]
    # breadth is an expected share (calibrated probability per app), so noise never makes it exactly 1/40
    assert f[("breadth", "ai chat")] < 0.1
    assert f[("breadth", "habit tracker")] > 4 * f[("breadth", "ai chat")]
    assert f[("attainable", "ai chat")] < 0 < f[("attainable", "habit tracker")]
    assert f[("entrant_share", "habit tracker")] == 1.0 and f[("entrant_share", "ai chat")] == 0.0
    assert f[("indie_share", "habit tracker")] == 1.0
    # the leader's size must not rescue the concentrated sector
    assert f[("monetization", "ai chat")] > f[("monetization", "habit tracker")]


def test_composite_marks_concentrated_sectors_excluded(con, monkeypatch):
    import json

    import apptopics.metrics.sector as sec
    from apptopics.pipeline.score import composite
    monkeypatch.setattr(sec, "estimate_app_revenue", lambda cfg, asof, rc, rcc, rel, ver, best, observed=None: (float(rc or 0) * 0.5, "ratings"))
    _sector(con, "ai chat", [2_000_000] + [200] * 39)
    _sector(con, "habit tracker", [40_000 - 1_000 * i for i in range(40)], new_from=3)
    run_metrics(con, "us", D, only=["breadth", "attainable", "concentration"])
    assert composite(con, "us", D) == 2
    meta = {k: json.loads(m) for k, m in con.execute("SELECT entity_id, meta FROM factor_values WHERE metric='composite'").fetchall()}
    assert meta["ai chat"]["excluded"] and "concentration" in meta["ai chat"]["excluded_reason"]
    assert "concentration" not in (meta["habit tracker"]["excluded_reason"] or "")   # may still be excluded for low coverage


def test_low_coverage_is_excluded(con):
    import json

    from apptopics import db
    from apptopics.pipeline.score import composite
    db.upsert(con, "factor_values", [{"entity_type": "keyword", "entity_id": "twitter", "metric": "demand", "value": 99.0, "meta": None,
                                      "snapshot_date": D, "computed_at": None},
                                     {"entity_type": "keyword", "entity_id": "x", "metric": "demand", "value": 5.0, "meta": None,
                                      "snapshot_date": D, "computed_at": None}])
    assert composite(con, "us", D) == 2
    meta = json.loads(con.execute("SELECT meta FROM factor_values WHERE metric='composite' AND entity_id='twitter'").fetchone()[0])
    assert meta["excluded"] and "coverage" in meta["excluded_reason"]


def test_non_latin_keywords_are_excluded_and_not_selected(con):
    import json

    from apptopics import db
    from apptopics.pipeline.collect import is_latin, select_keywords
    from apptopics.pipeline.score import composite
    assert is_latin("hiit timer") and is_latin("café") and not is_latin("العاب") and not is_latin("игры")
    db.upsert(con, "keywords", [{"store": "us", "keyword": k, "genre": "g", "popularity": p, "rank_in_store": i, "snapshot_date": D}
                                for i, (k, p) in enumerate([("العاب", 90), ("hiit timer", 40)], 1)])
    assert select_keywords(con, "us", 10) == ["hiit timer"]
    db.upsert(con, "factor_values", [{"entity_type": "keyword", "entity_id": "العاب", "metric": "demand", "value": 90.0, "meta": None,
                                      "snapshot_date": D, "computed_at": None}])
    composite(con, "us", D)
    meta = json.loads(con.execute("SELECT meta FROM factor_values WHERE metric='composite' AND entity_id='العاب'").fetchone()[0])
    assert "non-latin script" in meta["excluded_reason"]


def test_game_sectors_and_excluded_terms(con):
    import json

    from apptopics import db
    from apptopics.pipeline.collect import select_keywords
    from apptopics.pipeline.score import composite
    db.upsert(con, "keywords", [{"store": "us", "keyword": k, "genre": "g", "popularity": 50, "rank_in_store": i, "snapshot_date": D}
                                for i, k in enumerate(["mahjong", "cat vpn", "hiit timer"], 1)])
    assert select_keywords(con, "us", 10) == ["hiit timer", "mahjong"]      # vpn skipped before searching
    for i in range(1, 11):
        db.upsert(con, "search_results", [{"store": "us", "platform": "iphone", "keyword": "mahjong", "rank": i, "app_id": f"g{i}", "name": f"Mahjong {i}",
                                           "developer": f"d{i}", "genre": "Games", "rating_count": 1000, "avg_rating": 4.5, "subtitle": "", "total_results": 50, "snapshot_date": D}])
        db.upsert(con, "apps", [{"store": "us", "app_id": f"g{i}", "name": f"Mahjong {i}", "developer": f"d{i}", "bundle_id": "b", "primary_genre_id": "6014",
                                 "genre_ids": [], "price": 0.0, "currency": "USD", "release_date": None, "current_version_date": None, "rating_count": 1000,
                                 "avg_rating": 4.5, "rating_count_current": 0, "min_os": "17", "file_size_bytes": 0, "description": "", "snapshot_date": D}])
    db.upsert(con, "factor_values", [{"entity_type": "keyword", "entity_id": k, "metric": "demand", "value": 50.0, "meta": None, "snapshot_date": D, "computed_at": None}
                                     for k in ("mahjong", "cat vpn")])
    composite(con, "us", D)
    meta = {k: json.loads(m) for k, m in con.execute("SELECT entity_id, meta FROM factor_values WHERE metric='composite'").fetchall()}
    assert "games" in meta["mahjong"]["excluded_reason"] and "excluded term" in meta["cat vpn"]["excluded_reason"]
