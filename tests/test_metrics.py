import datetime as dt

from conftest import seed

from apptopics.metrics import metrics
from apptopics.metrics.brandness import name_similarity
from apptopics.pipeline.score import composite, run_metrics


def test_name_similarity():
    assert name_similarity("duolingo", "Duolingo: Learn Languages") == 1.0
    assert name_similarity("prize picks", "PrizePicks - DFS Game") == 1.0
    assert name_similarity("pokemon", "Pokémon GO") == 1.0
    assert name_similarity("hiit timer", "Interval Timer HIIT Timer") == 1.0
    assert name_similarity("snack tracker", "Food Diary") == 0.0


def test_metrics_and_composite(con, today):
    seed(con, today)
    counts = run_metrics(con, "us", today)
    assert counts["demand"] == 3 and counts["brandness"] == 3 and counts["competition"] == 3 and counts["monetization"] == 3
    assert counts["difficulty"] == 3 and counts["play_demand"] == 0  # difficulty is computed from the sector; play has no data -> skipped
    vals = {(m, e): v for e, m, v in con.execute("SELECT entity_id, metric, value FROM factor_values").fetchall()}
    assert vals[("brandness", "instagram")] > 0.9
    assert vals[("brandness", "snack tracker")] < 0.3
    assert vals[("brandness", "hiit timer")] < vals[("brandness", "instagram")]
    assert vals[("competition", "instagram")] > vals[("competition", "hiit timer")] > vals[("competition", "snack tracker")]
    assert vals[("monetization", "instagram")] > vals[("monetization", "hiit timer")] > vals[("monetization", "snack tracker")]
    assert composite(con, "us", today) == 3
    comp = dict(con.execute("SELECT entity_id, value FROM factor_values WHERE metric='composite'").fetchall())
    # brand penalty must sink instagram despite top demand
    assert comp["instagram"] < comp["hiit timer"]


def test_momentum_uses_history(con, today):
    seed(con, today - dt.timedelta(days=7))
    seed(con, today)
    con.execute("UPDATE keywords SET popularity=60 WHERE keyword='hiit timer' AND snapshot_date=?", [today])
    rows = {r.entity_id: r for r in metrics["momentum"]().compute(con, "us", today)}
    assert rows["hiit timer"].meta["dpop_7d"] == 20 and rows["hiit timer"].meta["has_history"]
    assert rows["hiit timer"].value > rows["instagram"].value



def test_own_difficulty_orders_sectors():
    from apptopics.metrics.difficulty import own_difficulty
    giant = own_difficulty([5_000_000, 2_000_000, 900_000, 500_000, 100_000, 90_000, 50_000, 40_000, 30_000, 20_000], [3000] * 10)
    niche = own_difficulty([2_000, 900, 500, 300, 120, 80, 40, 20, 10, 5], [200, 400, 100, 900, 50, None, None, None, None, None])
    assert 100 >= giant > 70 > niche > own_difficulty([], []) == 0


def test_difficulty_metric_without_astro(con, today):
    seed(con, today)
    rows = {r.entity_id: r for r in metrics["difficulty"]().compute(con, "us", today)}
    assert rows["instagram"].value > rows["hiit timer"].value > rows["snack tracker"].value
    assert rows["instagram"].meta["source"] == "own"


def test_navigational_giant_with_other_name(con, today):
    from apptopics import db
    seed(con, today)
    db.upsert(con, "search_results", [
        {"store": "us", "platform": "iphone", "keyword": "twitter", "rank": 1, "app_id": "t1", "name": "X", "developer": "X Corp.", "genre": "g",
         "rating_count": 12_000_000, "avg_rating": 4.1, "subtitle": "", "total_results": 50, "snapshot_date": today},
        {"store": "us", "platform": "iphone", "keyword": "twitter", "rank": 2, "app_id": "t2", "name": "Threads", "developer": "Meta", "genre": "g",
         "rating_count": 1_500_000, "avg_rating": 4.5, "subtitle": "", "total_results": 50, "snapshot_date": today}])
    rows = {r.entity_id: r for r in metrics["brandness"]().compute(con, "us", today)}
    assert rows["twitter"].value > 0.6 and rows["hiit timer"].value < 0.6


def test_brand_family_and_generic_stuffing(con, today):
    from apptopics import db
    rows = []
    fam = [("Amazon Shopping", "AMZN Mobile LLC", 9_000_000), ("Amazon Prime Video", "AMZN Mobile LLC", 3_000_000), ("Amazon Music", "AMZN Mobile LLC", 2_000_000),
           ("Amazon Alexa", "AMZN Mobile LLC", 1_500_000), ("Amazon Photos", "AMZN Mobile LLC", 800_000), ("Temu", "Temu", 2_500_000)]
    for i, (n, d, rc) in enumerate(fam, 1):
        rows.append({"store": "us", "platform": "iphone", "keyword": "amazon", "rank": i, "app_id": f"am{i}", "name": n, "developer": d, "genre": "g",
                     "rating_count": rc, "avg_rating": 4.5, "subtitle": "", "total_results": 50, "snapshot_date": today})
    gen = [("Sleep Sounds: White Noise", "A", 50_000), ("White Noise Lite", "B", 278_000), ("Rain Rain Sleep Sounds", "C", 137_000),
           ("White Noise Deluxe", "D", 9_000), ("Calm", "Calm.com", 1_990_000)]
    for i, (n, d, rc) in enumerate(gen, 1):
        rows.append({"store": "us", "platform": "iphone", "keyword": "white noise", "rank": i, "app_id": f"wn{i}", "name": n, "developer": d, "genre": "g",
                     "rating_count": rc, "avg_rating": 4.5, "subtitle": "", "total_results": 50, "snapshot_date": today})
    db.upsert(con, "search_results", rows)
    r = {x.entity_id: x.value for x in metrics["brandness"]().compute(con, "us", today)}
    assert r["amazon"] > 0.6          # same-developer family under the keyword's name
    assert r["white noise"] < 0.6     # keyword carried by many developers = generic need
