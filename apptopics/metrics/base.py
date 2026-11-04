from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, ClassVar

import duckdb


@dataclass
class FactorRow:
    entity_id: str
    value: float
    meta: dict[str, Any] | None = None
    entity_type: str | None = None  # defaults to the metric's entity_type


class Metric:
    """Base class for a metric plugin.

    Subclasses set `name`, `description`, `entity_type` and implement `compute`,
    returning one FactorRow per entity for the given store/snapshot_date.
    `requires` lists tables that must have data; the scorer skips the metric
    (with a note) if they are empty rather than failing the whole run.
    """

    name: ClassVar[str]
    description: ClassVar[str] = ""
    entity_type: ClassVar[str] = "keyword"
    requires: ClassVar[tuple[str, ...]] = ()
    higher_is_better: ClassVar[bool | None] = None

    def compute(self, con: duckdb.DuckDBPyConnection, store: str, snapshot_date: dt.date) -> list[FactorRow]:
        raise NotImplementedError


def latest_snapshot(con, table: str, store_col: str, store: str, on_or_before: dt.date, date_col: str = "snapshot_date"):
    row = con.execute(
        f"SELECT max({date_col}) FROM {table} WHERE {store_col} = ? AND {date_col} <= ?", [store, on_or_before]
    ).fetchone()
    if table == "search_results":   # metrics read sr_latest (newest page per keyword); build it on first use
        try:
            con.execute("SELECT 1 FROM sr_latest LIMIT 0")
        except Exception:
            ensure_sr_latest(con, on_or_before)
    return row[0] if row else None


def has_rows(con, table: str) -> bool:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0


def ensure_sr_latest(con, snapshot_date=None) -> None:
    """Per-connection temp table: each keyword's newest search page (searches happen on different days:
    the universe was searched on one day, long-tail terms later). snapshot_date is overwritten with the max
    date so metrics can keep filtering on `snapshot_date = latest_snapshot(search_results)`."""
    d = snapshot_date or con.execute("SELECT max(snapshot_date) FROM search_results").fetchone()[0]
    mx = con.execute("SELECT max(snapshot_date) FROM search_results WHERE snapshot_date <= ?", [d]).fetchone()[0]
    if mx is None:
        con.execute("CREATE OR REPLACE TEMP TABLE sr_latest AS SELECT * FROM search_results WHERE false")
        return
    con.execute(f"""CREATE OR REPLACE TEMP TABLE sr_latest AS
        SELECT * REPLACE (DATE '{mx}' AS snapshot_date) FROM search_results WHERE snapshot_date <= DATE '{mx}'
        QUALIFY dense_rank() OVER (PARTITION BY store, keyword ORDER BY snapshot_date DESC) = 1""")
