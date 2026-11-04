"""DuckDB storage. Every table is snapshot-oriented (snapshot_date) so metrics can
be computed as time series and backtested. factor_values is the generic,
extensible sink for any metric plugin."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
  store TEXT, keyword TEXT, genre TEXT, popularity INTEGER, rank_in_store INTEGER,
  snapshot_date DATE, PRIMARY KEY (store, keyword, snapshot_date));

CREATE TABLE IF NOT EXISTS keyword_metrics (
  store TEXT, keyword TEXT, popularity INTEGER, difficulty INTEGER, apps_count INTEGER,
  source TEXT, snapshot_date DATE, PRIMARY KEY (store, keyword, source, snapshot_date));

CREATE TABLE IF NOT EXISTS search_results (
  store TEXT, platform TEXT, keyword TEXT, rank INTEGER, app_id TEXT, name TEXT, developer TEXT,
  genre TEXT, rating_count BIGINT, avg_rating DOUBLE, subtitle TEXT, total_results INTEGER,
  snapshot_date DATE, PRIMARY KEY (store, platform, keyword, rank, snapshot_date));

CREATE TABLE IF NOT EXISTS chart_entries (
  store TEXT, chart TEXT, genre_id TEXT, rank INTEGER, app_id TEXT, name TEXT, developer TEXT,
  price DOUBLE, release_date DATE, snapshot_date DATE,
  PRIMARY KEY (store, chart, genre_id, rank, snapshot_date));

CREATE TABLE IF NOT EXISTS apps (
  store TEXT, app_id TEXT, name TEXT, developer TEXT, bundle_id TEXT, primary_genre_id TEXT,
  genre_ids TEXT[], price DOUBLE, currency TEXT, release_date TIMESTAMP, current_version_date TIMESTAMP,
  rating_count BIGINT, avg_rating DOUBLE, rating_count_current BIGINT, min_os TEXT,
  file_size_bytes BIGINT, description TEXT, snapshot_date DATE,
  PRIMARY KEY (store, app_id, snapshot_date));

CREATE TABLE IF NOT EXISTS play_search (
  country TEXT, keyword TEXT, rank INTEGER, package TEXT, title TEXT, developer TEXT,
  score DOUBLE, free BOOLEAN, price_text TEXT, snapshot_date DATE,
  PRIMARY KEY (country, keyword, rank, snapshot_date));

CREATE TABLE IF NOT EXISTS play_apps (
  country TEXT, package TEXT, title TEXT, developer TEXT, genre TEXT, genre_id TEXT,
  score DOUBLE, ratings BIGINT, reviews BIGINT, installs_text TEXT, min_installs BIGINT,
  max_installs BIGINT, price DOUBLE, free BOOLEAN, offers_iap BOOLEAN, released TEXT,
  updated TIMESTAMP, snapshot_date DATE, PRIMARY KEY (country, package, snapshot_date));

CREATE TABLE IF NOT EXISTS topics (
  topic_id TEXT PRIMARY KEY, label TEXT, description TEXT, created_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS topic_keywords (
  topic_id TEXT, store TEXT, keyword TEXT, weight DOUBLE, PRIMARY KEY (topic_id, store, keyword));

CREATE TABLE IF NOT EXISTS factor_values (
  entity_type TEXT, entity_id TEXT, metric TEXT, value DOUBLE, meta JSON,
  snapshot_date DATE, computed_at TIMESTAMP,
  PRIMARY KEY (entity_type, entity_id, metric, snapshot_date));

CREATE TABLE IF NOT EXISTS longtail_keywords (
  store TEXT, keyword TEXT, seed TEXT, source TEXT, hint_rank INTEGER, popularity INTEGER, discovered_at DATE,
  PRIMARY KEY (store, keyword));

CREATE TABLE IF NOT EXISTS keyword_labels (
  store TEXT, keyword TEXT, label TEXT, p_brand DOUBLE, reason TEXT, model TEXT, labeled_at TIMESTAMP,
  PRIMARY KEY (store, keyword));

CREATE TABLE IF NOT EXISTS llm_votes (
  entity_type TEXT, entity_id TEXT, model TEXT, persona TEXT, score DOUBLE, rationale TEXT,
  snapshot_date DATE, PRIMARY KEY (entity_type, entity_id, model, persona, snapshot_date));

CREATE TABLE IF NOT EXISTS calibration_anchors (
  app_id TEXT, store TEXT, source TEXT, monthly_revenue_usd DOUBLE, monthly_downloads BIGINT,
  observed_at DATE, note TEXT, PRIMARY KEY (app_id, store, source, observed_at));

CREATE TABLE IF NOT EXISTS predictions (
  cycle_id TEXT, entity_type TEXT, entity_id TEXT, metric TEXT, predicted DOUBLE,
  horizon_days INTEGER, made_at DATE, realized DOUBLE, scored_at DATE,
  PRIMARY KEY (cycle_id, entity_type, entity_id, metric));

CREATE TABLE IF NOT EXISTS own_sales (
  report_date DATE, app_id TEXT, parent_id TEXT, sku TEXT, title TEXT, product_type TEXT, kind TEXT, units INTEGER,
  proceeds_unit DOUBLE, currency TEXT, proceeds_usd DOUBLE, country TEXT, subscription TEXT, period TEXT, row_no INTEGER,
  PRIMARY KEY (report_date, row_no));

CREATE TABLE IF NOT EXISTS keyword_volume (
  keyword TEXT, location TEXT, search_volume BIGINT, cpc DOUBLE, competition DOUBLE, competition_index INTEGER,
  monthly JSON, snapshot_date DATE, PRIMARY KEY (keyword, location, snapshot_date));

CREATE TABLE IF NOT EXISTS trends (
  keyword TEXT, location TEXT, reference TEXT, points JSON, rel_to_reference DOUBLE, slope_12m DOUBLE,
  snapshot_date DATE, PRIMARY KEY (keyword, location, snapshot_date));

CREATE TABLE IF NOT EXISTS reviews (
  store TEXT, app_id TEXT, review_id TEXT, rating INTEGER, title TEXT, body TEXT, version TEXT, updated TIMESTAMP,
  fetched_date DATE, PRIMARY KEY (store, app_id, review_id));

CREATE TABLE IF NOT EXISTS play_reviews (
  country TEXT, package TEXT, review_id TEXT, rating DOUBLE, body TEXT, version TEXT, helpful INTEGER, posted TIMESTAMP,
  fetched_date DATE, PRIMARY KEY (country, package, review_id));

CREATE TABLE IF NOT EXISTS reddit_posts (
  id TEXT PRIMARY KEY, topic TEXT, aspect TEXT, query TEXT, subreddit TEXT, title TEXT, body TEXT, score INTEGER,
  num_comments INTEGER, created_utc TIMESTAMP, permalink TEXT, fetched_date DATE);

CREATE TABLE IF NOT EXISTS reddit_comments (
  id TEXT PRIMARY KEY, post_id TEXT, topic TEXT, aspect TEXT, subreddit TEXT, body TEXT, score INTEGER,
  created_utc TIMESTAMP, permalink TEXT, fetched_date DATE);

CREATE TABLE IF NOT EXISTS social_posts (
  id TEXT PRIMARY KEY, platform TEXT, topic TEXT, aspect TEXT, query TEXT, title TEXT, views BIGINT, likes BIGINT,
  comments BIGINT, created_utc TIMESTAMP, url TEXT, fetched_date DATE);

CREATE TABLE IF NOT EXISTS social_comments (
  id TEXT PRIMARY KEY, platform TEXT, post_id TEXT, topic TEXT, aspect TEXT, body TEXT, likes BIGINT,
  created_utc TIMESTAMP, fetched_date DATE);

CREATE TABLE IF NOT EXISTS ad_library (
  ad_id TEXT PRIMARY KEY, app_id TEXT, page_id TEXT, page_name TEXT, title TEXT, body TEXT, start_date DATE,
  is_active BOOLEAN, platforms TEXT, link_url TEXT, fetched_date DATE);

CREATE TABLE IF NOT EXISTS app_iap (
  store TEXT, app_id TEXT, position INTEGER, item TEXT, price_usd DOUBLE, period TEXT, fetched_date DATE,
  PRIMARY KEY (store, app_id, position, fetched_date));

CREATE TABLE IF NOT EXISTS app_iap_checked (
  store TEXT, app_id TEXT, fetched_date DATE, n_items INTEGER, PRIMARY KEY (store, app_id, fetched_date));

CREATE TABLE IF NOT EXISTS play_charts (
  country TEXT, collection TEXT, category TEXT, rank INTEGER, package TEXT, title TEXT, developer TEXT, free BOOLEAN,
  snapshot_date DATE, PRIMARY KEY (country, collection, category, rank, snapshot_date));

CREATE TABLE IF NOT EXISTS popularity_weekly (
  country TEXT, genre TEXT, term TEXT, week DATE, rank_in_genre INTEGER, pop_in_genre INTEGER, pop_1to100 INTEGER, pop_1to5 INTEGER,
  PRIMARY KEY (country, genre, term, week));

CREATE TABLE IF NOT EXISTS collection_log (
  job TEXT, store TEXT, started_at TIMESTAMP, finished_at TIMESTAMP, rows INTEGER, ok BOOLEAN, error TEXT);

CREATE TABLE IF NOT EXISTS keyword_social (
  keyword TEXT, source TEXT, fetched_date DATE, views BIGINT, median_views BIGINT, videos INTEGER,
  ads_checked INTEGER, ads_verified INTEGER, ads_oldest_days INTEGER, PRIMARY KEY (keyword, source, fetched_date));

CREATE TABLE IF NOT EXISTS topic_social (
  topic_id TEXT, fetched_date DATE, tiktok_videos INTEGER, tiktok_views BIGINT, tiktok_median_views BIGINT,
  youtube_videos INTEGER, youtube_views BIGINT, youtube_median_views BIGINT, apps_checked INTEGER, apps_with_ads INTEGER,
  apps_verified_ads INTEGER, max_active_ads INTEGER, max_oldest_active_days INTEGER, ad_apps TEXT, credits INTEGER,
  PRIMARY KEY (topic_id, fetched_date));
"""


def today() -> dt.date:
    return dt.date.today()


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = Path(path or settings.db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def read_copy_path(path: Path | None = None) -> Path:
    p = Path(path or settings.db_path)
    return p.with_name(p.stem + ".read" + p.suffix)


def snapshot_copy(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> Path:
    """Write a consistent read-only copy next to the main file. The full run calls this at
    the end so the dashboard keeps working while the next run holds the write lock."""
    dst = read_copy_path(path)
    tmp = dst.with_suffix(".tmp.duckdb")
    for f in (tmp, tmp.with_suffix(".duckdb.wal")):
        if f.exists():
            f.unlink()
    con.execute(f"ATTACH '{tmp}' AS snap")
    main = con.execute("SELECT current_database()").fetchone()[0]
    con.execute(f"COPY FROM DATABASE {main} TO snap")
    con.execute("DETACH snap")
    tmp.replace(dst)
    return dst


def connect_read_live(path: Path | None = None, tries: int = 120, wait: float = 5.0) -> duckdb.DuckDBPyConnection:
    """Read-only connection to the MAIN db for pipeline steps: waits for a writer instead of falling back to the
    (possibly stale) dashboard copy."""
    import time
    p = Path(path or settings.db_path)
    for i in range(tries):
        try:
            return duckdb.connect(str(p), read_only=True)
        except (duckdb.IOException, duckdb.ConnectionException):
            if i == tries - 1:
                raise
            time.sleep(wait)


def connect_read(path: Path | None = None) -> tuple[duckdb.DuckDBPyConnection, bool]:
    """Read-only connection to the main db, falling back to the last snapshot copy when a
    writer holds the lock. Returns (con, is_fallback)."""
    p = Path(path or settings.db_path)
    try:
        return duckdb.connect(str(p), read_only=True), False
    except (duckdb.IOException, duckdb.ConnectionException):  # another process holds the write lock / same-process writer open
        rc = read_copy_path(p)
        if not rc.exists():
            raise
        return duckdb.connect(str(rc), read_only=True), True




BULK_MIN = 1000   # executemany costs ~1 ms per row in DuckDB; above this, load through a temporary JSON-lines file


def _scalar_rows(rows: list[dict]) -> bool:
    return all(not isinstance(v, (list, tuple, dict, set, bytes)) for r in rows for v in r.values())


def upsert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> int:
    """Insert rows, replacing on primary-key conflict. Rows are dicts with identical keys.
    Large batches of scalar values go through DuckDB's JSON reader in one statement (100x faster than executemany);
    every value is read as text and cast to the column type by the INSERT."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if len(rows) >= BULK_MIN and _scalar_rows(rows):
        import json as _json
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jsonl", prefix=f"upsert_{table}_")
        try:
            with os.fdopen(fd, "w") as f:
                for r in rows:
                    f.write(_json.dumps({c: (None if r.get(c) is None else str(r.get(c)) if not isinstance(r.get(c), bool) else str(r.get(c)).lower())
                                         for c in cols}))
                    f.write("\n")
            spec = "{" + ",".join(f"'{c}':'VARCHAR'" for c in cols) + "}"
            con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) SELECT {','.join(cols)} FROM "
                        f"read_json(?, format='newline_delimited', columns={spec})", [path])
        finally:
            os.unlink(path)
        return len(rows)
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    con.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def log_job(con, job: str, store: str, started, rows: int, ok: bool, error: str | None = None):
    con.execute(
        "INSERT INTO collection_log VALUES (?,?,?,?,?,?,?)",
        [job, store, started, dt.datetime.now(), rows, ok, error],
    )
