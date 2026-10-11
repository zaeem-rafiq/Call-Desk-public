"""Tiger Data reaction: SQL numbers, display SQL, storage stats and self_check(), all with a fake psycopg connection."""

import asyncio
import contextlib
from datetime import datetime, timedelta

import pandas as pd
import psycopg
import pytest

from app import config
from app.tools import prices

SINCE = "2026-10-08T08:20:17-04:00"
PRIOR = {"PEP": 99.0, "XLP": 49.5}
DAY = {"PEP": 101.0, "XLP": 50.5}


@pytest.fixture(autouse=True)
def _fresh_storage_cache():
    prices._storage_cache.clear()
    yield
    prices._storage_cache.clear()


def _bars() -> list[tuple]:
    t0 = datetime.fromisoformat("2026-10-08T08:00:00-04:00")
    rows = []
    for i in range(90):
        ts = t0 + timedelta(minutes=i)
        rows.append((ts, "PEP", 100 + 0.1 * i))
        if i >= 5:  # the benchmark starts later, like the real pre-market data
            rows.append((ts, "XLP", 50 + 0.02 * i))
    return rows


def _pandas_reaction() -> dict:
    bars = pd.DataFrame(_bars(), columns=["ts", "ticker", "close"])
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    return prices.compute_reaction(bars, PRIOR, DAY, "PEP", "XLP", prices._parse_ts(SINCE), 30, "pandas", False).model_dump()


class FakeConn:
    """Answers each query the live path sends, by its text. Records what ran."""

    def __init__(self, reaction_row, columnstore_fn_exists=False):
        self.reaction_row = reaction_row
        self.columnstore_fn_exists = columnstore_fn_exists
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextlib.contextmanager
    def pipeline(self):
        yield

    def rollback(self):
        self.queries.append("ROLLBACK")

    def execute(self, sql, params=None):
        self.queries.append(sql)
        if sql.startswith("EXPLAIN"):
            result = [([{"Planning Time": 0.7, "Execution Time": 1.6}],)]
        elif sql == prices.REACTION_SQL:
            assert params["ticker"] == "PEP" and params["benchmark"] == "XLP"
            result = [self.reaction_row]
        elif "FROM bars_1m" in sql:
            result = _bars()
        elif "FROM reference_close" in sql:
            result = [("prior", t, c) for t, c in PRIOR.items()] + [("day", t, c) for t, c in DAY.items()]
        elif "hypertable_columnstore_stats" in sql:
            if not self.columnstore_fn_exists:
                raise psycopg.errors.UndefinedFunction("function hypertable_columnstore_stats does not exist")
            result = [(1, 0, None, None)]
        elif "hypertable_compression_stats" in sql:
            result = [(3, 2, 9000, 1000)]
        elif "hypertable_size" in sql:
            result = [(12345,)]
        else:
            raise AssertionError(f"unexpected query: {sql[:60]}")
        cur = type("Cur", (), {})()
        cur.fetchone = lambda: result[0] if result else None
        cur.fetchall = lambda: result
        return cur


def _use_fake(monkeypatch, conn: FakeConn) -> None:
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(config, "get", lambda name, default=None: {"TIGER_DATABASE_URL": "postgresql://fake"}.get(name, default))


def _row_from(reaction: dict) -> tuple:
    return tuple(reaction[k] for k in prices.SQL_FIELDS)


def test_display_sql_inlines_literals_and_escapes_quotes():
    params = prices._reaction_params("O'X", "XLP", prices._parse_ts(SINCE))
    sql = prices.display_sql(params)
    assert "%(" not in sql
    assert "'O''X'" in sql and "'XLP'" in sql
    assert "'2026-10-08 08:20:17-04'" in sql and "'2026-10-08 08:50:17-04'" in sql  # quote and window end, in ET
    assert "session_day = '2026-10-08'" in sql


def test_sql_numbers_follow_the_pandas_null_rules():
    full = (1.0, 0.5, 0.5, 2.0, None, 3.0, 1.0, None)
    out = prices._sql_numbers(full, {"ticker": "PEP", "benchmark": "XLP"})
    assert out["day_pct_move"] is None  # pandas gives a day move only when both prior closes exist
    assert out["close_pct_move"] is None and out["close_benchmark_pct_move"] is None
    assert out["pct_move"] == 1.0 and out["vs_benchmark"] == 0.5
    with pytest.raises(ValueError):
        prices._sql_numbers(None, {"ticker": "PEP", "benchmark": "XLP"})


def test_live_reaction_uses_sql_numbers_and_caches_storage(monkeypatch):
    sql_row = (-0.8341, 0.0734, -0.9075, 0.8651, 0.4362, 3.7261, 2.1052, 1.6209)  # what Tiger returned
    conn = FakeConn(sql_row)
    _use_fake(monkeypatch, conn)

    r = asyncio.run(prices.live_price_reaction("PEP", SINCE, "XLP"))
    assert [r[k] for k in prices.SQL_FIELDS] == [round(v, 3) for v in sql_row]  # SQL numbers, not the pandas ones
    assert r["sql"] == prices.display_sql(prices._reaction_params("PEP", "XLP", prices._parse_ts(SINCE)))
    assert r["query_ms"] == 2  # planning 0.7 + execution 1.6 ms
    assert r["storage"] == {"chunks": 3, "compressed_chunks": 2, "before_bytes": 9000, "after_bytes": 1000, "ratio": 9.0,
                            "total_bytes": 12345}  # fell back to hypertable_compression_stats
    assert r["series"] and not r["mock"] and "Tiger" in r["source"]
    assert prices.REACTION_SQL in conn.queries  # executed with bound parameters, not the display text
    assert r["sql"] not in conn.queries

    conn.queries.clear()
    asyncio.run(prices.live_price_reaction("PEP", SINCE, "XLP"))
    assert not any("stats" in q or "hypertable_size" in q for q in conn.queries)  # storage read once per process


def test_storage_without_compressed_chunks_has_no_ratio(monkeypatch):
    _use_fake(monkeypatch, FakeConn(_row_from(_pandas_reaction()), columnstore_fn_exists=True))
    r = asyncio.run(prices.live_price_reaction("PEP", SINCE, "XLP"))
    assert r["storage"]["chunks"] == 1 and r["storage"]["compressed_chunks"] == 0 and r["storage"]["ratio"] is None


def test_self_check_passes_when_sql_matches_pandas(monkeypatch):
    _use_fake(monkeypatch, FakeConn(_row_from(_pandas_reaction())))
    rows = asyncio.run(prices.self_check())
    assert [(name, ok) for name, ok, _ in rows] == [
        ("SQL reaction matches pandas", True), ("reaction query under 500 ms", True), ("columnstore stats present", True)]


def test_self_check_flags_a_mismatch(monkeypatch):
    row = list(_row_from(_pandas_reaction()))
    row[2] += 0.05  # abnormal move off by 0.05 pp
    _use_fake(monkeypatch, FakeConn(tuple(row)))
    rows = dict((name, ok) for name, ok, _ in asyncio.run(prices.self_check()))
    assert rows["SQL reaction matches pandas"] is False


def test_self_check_never_raises(monkeypatch):
    def refuse(*a, **k):
        raise psycopg.OperationalError("connection refused")

    _use_fake(monkeypatch, FakeConn(None))
    monkeypatch.setattr(psycopg, "connect", refuse)
    rows = asyncio.run(prices.self_check())
    assert len(rows) == 1 and rows[0][1] is False and "OperationalError" in rows[0][2]

    monkeypatch.setattr(config, "get", lambda name, default=None: default)
    assert asyncio.run(prices.self_check())[0][1] is False


@pytest.mark.skipif(not (config.DATA / "prices.csv").exists(), reason="vendor price data is not redistributed")
def test_mock_reaction_numbers_unchanged_and_shows_the_query():
    r = asyncio.run(prices.mock_price_reaction("PEP", SINCE, "XLP"))
    bars, prior, day = prices._csv_bars()
    ref = prices.compute_reaction(bars, prior, day, "PEP", "XLP", prices._parse_ts(SINCE), prices._window_min(), "x", True).model_dump()
    assert [r[k] for k in prices.SQL_FIELDS] == [ref[k] for k in prices.SQL_FIELDS]
    assert r["mock"] and r["storage"] == {} and r["query_ms"] is None
    assert r["sql"].startswith("WITH bars AS") and "'PEP'" in r["sql"]
