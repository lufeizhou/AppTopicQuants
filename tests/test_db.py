import datetime as dt

from apptopics import db


def test_upsert_replaces(con):
    d = dt.date(2026, 1, 1)
    row = {"store": "us", "keyword": "a", "genre": "1", "popularity": 5, "rank_in_store": 1, "snapshot_date": d}
    db.upsert(con, "keywords", [row]); db.upsert(con, "keywords", [{**row, "popularity": 9}])
    assert con.execute("SELECT popularity FROM keywords").fetchall() == [(9,)]


def test_snapshot_copy_and_read_fallback(tmp_path):
    from apptopics import db
    main = tmp_path / "m.duckdb"
    con = db.connect(main)
    db.upsert(con, "keywords", [{"store": "us", "keyword": "a", "genre": "1", "popularity": 5, "rank_in_store": 1, "snapshot_date": dt.date(2026, 1, 1)}])
    dst = db.snapshot_copy(con, main)
    assert dst.exists() and dst.name == "m.read.duckdb"
    # while the writer holds the lock, a read must fall back to the copy
    rc, fallback = db.connect_read(main)
    assert fallback and rc.execute("SELECT count(*) FROM keywords").fetchone()[0] == 1
    rc.close(); con.close()
    rc, fallback = db.connect_read(main)
    assert not fallback
    rc.close()
