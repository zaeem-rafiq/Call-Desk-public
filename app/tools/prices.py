"""get_price_reaction: ticker move since a quote vs a benchmark (abnormal move).

Live: Tiger Data computes the reaction in one SQL query (REACTION_SQL) over the
`bars_1m` continuous aggregate. The sparkline comes from a plain bars query.
Mock: the same bars read from data/prices.csv. compute_reaction() is the pandas
reference: it gives the mock numbers and the live sparkline.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd

from .. import config
from ..models import PricePoint, PriceReaction

SERIES_LOOKBACK = timedelta(minutes=150)  # sparkline context before the quote
MAX_POINTS = 120


def _parse_ts(value: str | datetime) -> datetime:
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=config.ET)


def _session_close(since: datetime) -> datetime:
    return since.astimezone(config.ET).replace(hour=16, minute=0, second=0, microsecond=0)


def compute_reaction(
    bars: pd.DataFrame,
    prior_close: dict[str, float],
    day_close: dict[str, float],
    ticker: str,
    benchmark: str,
    since: datetime,
    window_min: int,
    source: str,
    mock: bool,
) -> PriceReaction:
    """bars: columns ts (tz-aware), ticker, close."""
    until = since + timedelta(minutes=window_min)
    px = bars.pivot_table(index="ts", columns="ticker", values="close").sort_index().ffill()
    for t in (ticker, benchmark):
        if t not in px.columns:
            raise ValueError(f"no bars for {t}")
    px = px[[ticker, benchmark]].dropna()
    if px.empty:
        raise ValueError("no overlapping bars")

    def at(ts: datetime) -> pd.Series:
        before = px[px.index <= ts]
        return before.iloc[-1] if not before.empty else px.iloc[0]

    p0, p1 = at(since), at(until)
    pct = (p1[ticker] / p0[ticker] - 1) * 100
    bpct = (p1[benchmark] / p0[benchmark] - 1) * 100

    day_pct = day_vs = None
    if ticker in prior_close and benchmark in prior_close:
        day_pct = (p1[ticker] / prior_close[ticker] - 1) * 100
        day_b = (p1[benchmark] / prior_close[benchmark] - 1) * 100
        day_vs = day_pct - day_b

    close_pct = close_b = close_vs = None
    if all(t in prior_close and t in day_close for t in (ticker, benchmark)):
        close_pct = (day_close[ticker] / prior_close[ticker] - 1) * 100
        close_b = (day_close[benchmark] / prior_close[benchmark] - 1) * 100
        close_vs = close_pct - close_b

    ref_t = prior_close.get(ticker) or px[ticker].iloc[0]
    ref_b = prior_close.get(benchmark) or px[benchmark].iloc[0]
    window = px[(px.index >= since - SERIES_LOOKBACK) & (px.index <= max(until, _session_close(since)))]
    step = max(1, len(window) // MAX_POINTS)
    series = [
        PricePoint(ts=ts.isoformat(), pct=round((r[ticker] / ref_t - 1) * 100, 3), bench_pct=round((r[benchmark] / ref_b - 1) * 100, 3))
        for ts, r in window.iloc[::step].iterrows()
    ]
    fmt = lambda d: d.astimezone(config.ET).strftime("%H:%M")  # noqa: E731
    return PriceReaction(
        ticker=ticker,
        benchmark=benchmark,
        pct_move=round(pct, 3),
        benchmark_pct_move=round(bpct, 3),
        vs_benchmark=round(pct - bpct, 3),
        window=f"{fmt(since)} → {fmt(until)} ET",
        since_ts=since.isoformat(),
        until_ts=until.isoformat(),
        day_pct_move=None if day_pct is None else round(day_pct, 3),
        day_vs_benchmark=None if day_vs is None else round(day_vs, 3),
        close_pct_move=None if close_pct is None else round(close_pct, 3),
        close_benchmark_pct_move=None if close_b is None else round(close_b, 3),
        close_vs_benchmark=None if close_vs is None else round(close_vs, 3),
        series=series,
        source=source,
        mock=mock,
    )


# ---- mock: local CSV ----------------------------------------------------
def _closes(path) -> dict[str, float]:
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return dict(zip(df["ticker"], df["close"].astype(float)))


@lru_cache(maxsize=8)
def _csv_bars(prices_path, prior_close_path, day_close_path) -> tuple[pd.DataFrame, dict[str, float], dict[str, float]]:
    """The active call's local bars and closes (data/calls/<id>/... or the PepsiCo files), cached per call."""
    bars = pd.read_csv(prices_path)
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    return bars[["ts", "ticker", "close"]], _closes(prior_close_path), _closes(day_close_path)


async def mock_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> dict:
    info = config.call_info()
    bars, prior, day = _csv_bars(info.prices_path, info.prior_close_path, info.day_close_path)
    params = _reaction_params(ticker, benchmark, _parse_ts(since_ts))
    r = compute_reaction(bars, prior, day, ticker, benchmark, params["since"], _window_min(), "local prices.csv (Tiger offline)", True)
    return r.model_copy(update={"sql": display_sql(params)}).model_dump()


# ---- the reaction query: run on Tiger, shown on the card ------------------
# Same semantics as compute_reaction(): as-of closes (last 1-minute bar at or before
# a time), starting from the first minute both tickers have a bar.
REACTION_SQL = """\
WITH bars AS (  -- 1-minute closes from the bars_1m continuous aggregate
  SELECT ticker, bucket, close FROM bars_1m
  WHERE ticker IN (%(ticker)s, %(benchmark)s)
    AND bucket BETWEEN %(lo)s AND %(hi)s
), t0 AS (      -- first minute with a bar for both tickers
  SELECT max(first_bar) AS ts
  FROM (SELECT min(bucket) AS first_bar FROM bars GROUP BY ticker) f
), px AS (      -- as-of closes at the quote and at the window end
  SELECT ticker,
    last(close, bucket) FILTER (WHERE bucket <= greatest(%(since)s, t0.ts)) AS p0,
    last(close, bucket) FILTER (WHERE bucket <= greatest(%(until)s, t0.ts)) AS p1
  FROM bars CROSS JOIN t0 GROUP BY ticker, t0.ts
), ref AS (     -- prior and official closes for the session
  SELECT ticker,
    max(close) FILTER (WHERE kind = 'prior') AS prior_close,
    max(close) FILTER (WHERE kind = 'day') AS day_close
  FROM reference_close WHERE session_day = %(day)s GROUP BY ticker
), m AS (       -- moves in percent, per ticker
  SELECT ticker,
    100 * (p1 / p0 - 1) AS win_move,
    100 * (p1 / prior_close - 1) AS day_move,
    100 * (day_close / prior_close - 1) AS close_move
  FROM px LEFT JOIN ref USING (ticker)
)
SELECT t.win_move, b.win_move, t.win_move - b.win_move AS abnormal_pp,
  t.day_move, t.day_move - b.day_move AS day_abnormal_pp,
  t.close_move, b.close_move, t.close_move - b.close_move AS close_abnormal_pp
FROM m t JOIN m b ON t.ticker = %(ticker)s AND b.ticker = %(benchmark)s"""
PEER_SQL = """\
WITH bars AS (  -- the company and its peers, 1-minute closes from bars_1m
  SELECT ticker, bucket, close FROM bars_1m
  WHERE ticker = ANY(%(tickers)s) AND bucket BETWEEN %(lo)s AND %(until)s
), px AS (      -- as-of close at the quote (or the first bar after it) and at the window end
  SELECT ticker,
    coalesce(last(close, bucket) FILTER (WHERE bucket <= %(since)s), first(close, bucket)) AS p0,
    last(close, bucket) FILTER (WHERE bucket <= %(until)s) AS p1
  FROM bars GROUP BY ticker
), m AS (SELECT ticker, 100 * (p1 / p0 - 1) AS move FROM px)
SELECT p.ticker, p.move, p.move - b.move AS vs_benchmark
FROM m p JOIN m b ON b.ticker = %(benchmark)s
WHERE p.ticker <> %(benchmark)s
ORDER BY p.move - b.move"""


def peer_params(params: dict, peers: tuple[str, ...]) -> dict:
    return {**params, "tickers": [params["ticker"], *peers, params["benchmark"]]}


def display_peer_sql(params: dict) -> str:
    shown = {**params, "tickers": "{" + ",".join(params["tickers"]) + "}"}
    return re.sub(r"%\((\w+)\)s", lambda m: _literal(shown[m.group(1)]), PEER_SQL)


def _peer_rows(rows: list[tuple], subject: str) -> list[dict]:
    return [{"ticker": t, "move": round(float(m), 3), "vs_benchmark": round(float(v), 3), "subject": t == subject}
            for t, m, v in rows if m is not None and v is not None]


def _call_peers() -> tuple[str, ...]:
    """The active call's peer tickers (call.json "peers"); none when no call is configured."""
    try:
        return tuple(getattr(config.call_info(), "peers", ()) or ())
    except Exception:  # noqa: BLE001 - peers are an extra panel; the reaction never depends on them
        return ()


SQL_FIELDS = ("pct_move", "benchmark_pct_move", "vs_benchmark", "day_pct_move", "day_vs_benchmark",
              "close_pct_move", "close_benchmark_pct_move", "close_vs_benchmark")


def _window_min() -> int:
    return int(config.get("REACTION_WINDOW_MIN") or 30)


def _reaction_params(ticker: str, benchmark: str, since: datetime) -> dict:
    until = since + timedelta(minutes=_window_min())
    return {"ticker": ticker, "benchmark": benchmark, "since": since, "until": until,
            "lo": since - SERIES_LOOKBACK, "hi": max(until, _session_close(since)), "day": since.astimezone(config.ET).date()}


def _literal(value: object) -> str:
    if isinstance(value, datetime):
        value = value.astimezone(config.ET).isoformat(sep=" ").removesuffix(":00")  # '2026-10-08 08:20:17-04'
    elif isinstance(value, date):
        value = value.isoformat()
    return "'" + str(value).replace("'", "''") + "'"


def display_sql(params: dict) -> str:
    """REACTION_SQL with literal values, for display only. Execution always binds parameters."""
    return re.sub(r"%\((\w+)\)s", lambda m: _literal(params[m.group(1)]), REACTION_SQL)


# ---- live: Tiger Data ---------------------------------------------------
_storage_cache: dict = {}


def _sql_numbers(row: tuple | None, params: dict) -> dict:
    if row is None or row[0] is None:
        raise ValueError(f"no overlapping bars for {params['ticker']} and {params['benchmark']}")
    out = {k: None if v is None else round(float(v), 3) for k, v in zip(SQL_FIELDS, row)}
    # Like compute_reaction(): day and close moves only when both tickers have their reference closes.
    if out["day_vs_benchmark"] is None:
        out["day_pct_move"] = None
    if out["close_vs_benchmark"] is None:
        out["close_pct_move"] = out["close_benchmark_pct_move"] = None
    return out


def _server_ms(plan: dict) -> int:
    """Planning + execution time from EXPLAIN (ANALYZE, FORMAT JSON): the query's time on Tiger, without network."""
    return max(1, round(plan.get("Planning Time", 0) + plan.get("Execution Time", 0)))


def _storage(conn) -> dict:
    """Columnstore stats of the price_ticks hypertable, read once per process. Display only: {} on failure."""
    if _storage_cache:
        return _storage_cache
    import psycopg

    try:
        stats = None
        for fn in ("hypertable_columnstore_stats", "hypertable_compression_stats"):  # TimescaleDB 2.18+ name first
            try:
                stats = conn.execute(f"SELECT total_chunks, number_compressed_chunks, before_compression_total_bytes, "
                                     f"after_compression_total_bytes FROM {fn}('price_ticks')").fetchone()
                break
            except psycopg.Error:
                conn.rollback()
        total = conn.execute("SELECT hypertable_size('price_ticks')").fetchone()[0]
    except psycopg.Error:
        conn.rollback()
        return {}
    chunks, compressed, before, after = stats or (None, None, None, None)
    _storage_cache.update(chunks=chunks, compressed_chunks=compressed, before_bytes=before, after_bytes=after,
                          ratio=round(before / after, 1) if before and after else None, total_bytes=total)
    return _storage_cache


def _tiger_fetch(params: dict, peers: tuple[str, ...] = ()) -> tuple[pd.DataFrame, dict[str, float], dict[str, float], dict, int, dict, list]:
    """One pipelined round trip: the reaction query, its server time, the sparkline bars, reference closes, peers."""
    import psycopg

    tickers = [params["ticker"], params["benchmark"]]
    with psycopg.connect(config.get("TIGER_DATABASE_URL"), connect_timeout=8) as conn:
        with conn.pipeline():
            peer_cur = conn.execute(PEER_SQL, peer_params(params, peers)) if peers else None
            reaction = conn.execute(REACTION_SQL, params)
            explain = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + REACTION_SQL, params)
            bars_cur = conn.execute(
                "SELECT bucket, ticker, close FROM bars_1m WHERE ticker = ANY(%s) AND bucket BETWEEN %s AND %s ORDER BY bucket",
                (tickers, params["lo"], params["hi"]),
            )
            closes_cur = conn.execute(
                "SELECT kind, ticker, close FROM reference_close WHERE ticker = ANY(%s) AND session_day = %s", (tickers, params["day"]),
            )
        peer_rows = _peer_rows(peer_cur.fetchall(), params["ticker"]) if peer_cur is not None else []
        numbers = _sql_numbers(reaction.fetchone(), params)
        ms = _server_ms(explain.fetchone()[0][0])
        rows, closes = bars_cur.fetchall(), closes_cur.fetchall()
        storage = dict(_storage(conn))
    bars = pd.DataFrame(rows, columns=["ts", "ticker", "close"])
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    bars["close"] = bars["close"].astype(float)
    prior = {t: float(c) for k, t, c in closes if k == "prior"}
    day = {t: float(c) for k, t, c in closes if k == "day"}
    return bars, prior, day, numbers, ms, storage, peer_rows


async def live_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> dict:
    params = _reaction_params(ticker, benchmark, _parse_ts(since_ts))
    peers = _call_peers()
    bars, prior, day, numbers, ms, storage, peer_rows = await asyncio.to_thread(_tiger_fetch, params, peers)
    # pandas builds the sparkline and labels; the numbers are the ones Tiger computed in SQL.
    r = compute_reaction(bars, prior, day, ticker, benchmark, params["since"], _window_min(), "Tiger Data · bars_1m continuous aggregate", False)
    extra = {"peers": peer_rows, "peers_sql": display_peer_sql(peer_params(params, peers))} if peers else {}
    return r.model_copy(update={**numbers, "sql": display_sql(params), "query_ms": ms, "storage": storage, **extra}).model_dump()


# ---- live self-check (scripts/verify_live.py --only tiger) ---------------
SELF_CHECK_ARGS = ("PEP", "XLP", "2026-10-08T08:20:17-04:00")


async def self_check() -> list[tuple[str, bool | None, str]]:
    """SQL vs pandas on the same live bars, reaction query time, columnstore stats. Never raises.

    ok=None is a WARN: the stats exist but no chunk has moved to the columnstore yet.
    """
    ticker, benchmark, since_ts = SELF_CHECK_ARGS
    rows: list[tuple[str, bool | None, str]] = []
    try:
        if not config.get("TIGER_DATABASE_URL"):
            return [("Tiger connection", False, "TIGER_DATABASE_URL is not set")]
        params = _reaction_params(ticker, benchmark, _parse_ts(since_ts))
        started = time.perf_counter()
        bars, prior, day, numbers, ms, storage, _ = await asyncio.to_thread(_tiger_fetch, params)
        wall_ms = round((time.perf_counter() - started) * 1000)
        ref = compute_reaction(bars, prior, day, ticker, benchmark, params["since"], _window_min(), "pandas", False).model_dump()
        mismatched = [k for k in SQL_FIELDS if (numbers[k] is None) != (ref[k] is None)]
        diffs = [abs(numbers[k] - ref[k]) for k in SQL_FIELDS if numbers[k] is not None and ref[k] is not None]
        worst = max(diffs, default=0.0)
        rows.append(("SQL reaction matches pandas", bool(diffs) and not mismatched and worst <= 0.01,
                     f"{len(diffs)} fields, max diff {worst:.4f} pp; {ticker} {numbers['pct_move']:+.2f}% vs {benchmark} "
                     f"{numbers['benchmark_pct_move']:+.2f}% = {numbers['vs_benchmark']:+.2f} pp"
                     + (f"; null mismatch: {', '.join(mismatched)}" if mismatched else "")))
        rows.append(("reaction query under 500 ms", ms < 500, f"{ms} ms on Tiger; whole fetch {wall_ms} ms incl. connect"))
        if storage.get("ratio"):
            ok, detail = True, f"{storage['ratio']}x smaller, {storage['compressed_chunks']}/{storage['chunks']} chunks compressed"
        elif storage.get("chunks") is not None:
            ok, detail = None, f"{storage['compressed_chunks']}/{storage['chunks']} chunks in the columnstore yet (policy has not run)"
        else:
            ok, detail = False, "no columnstore stats for price_ticks"
        rows.append(("columnstore stats present", ok, detail))
    except Exception as exc:  # noqa: BLE001 - a self-check reports, it never raises
        rows.append(("Tiger live query", False, f"{type(exc).__name__}: {exc}"[:120]))
    return rows
