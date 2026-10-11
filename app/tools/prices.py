"""get_price_reaction: ticker move since a quote vs a benchmark (abnormal move).

Live: 1-minute bars from the Tiger Data continuous aggregate `bars_1m`.
Mock: the same bars read from data/prices.csv. Both paths share compute_reaction().
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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
def _closes(name: str) -> dict[str, float]:
    path = config.DATA / name
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return dict(zip(df["ticker"], df["close"].astype(float)))


@lru_cache(maxsize=1)
def _csv_bars() -> tuple[pd.DataFrame, dict[str, float], dict[str, float]]:
    bars = pd.read_csv(config.DATA / "prices.csv")
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    return bars[["ts", "ticker", "close"]], _closes("prior_close.csv"), _closes("day_close.csv")


async def mock_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> dict:
    bars, prior, day = _csv_bars()
    window = int(config.get("REACTION_WINDOW_MIN") or 30)
    r = compute_reaction(bars, prior, day, ticker, benchmark, _parse_ts(since_ts), window, "local prices.csv (Tiger offline)", True)
    return r.model_dump()


# ---- live: Tiger Data ---------------------------------------------------
def _tiger_query(ticker: str, benchmark: str, since: datetime, window_min: int) -> tuple[pd.DataFrame, dict[str, float], dict[str, float]]:
    import psycopg

    start = since - SERIES_LOOKBACK
    end = max(since + timedelta(minutes=window_min), _session_close(since))
    with psycopg.connect(config.get("TIGER_DATABASE_URL"), connect_timeout=8) as conn:
        rows = conn.execute(
            "SELECT bucket, ticker, close FROM bars_1m WHERE ticker = ANY(%s) AND bucket BETWEEN %s AND %s ORDER BY bucket",
            ([ticker, benchmark], start, end),
        ).fetchall()
        closes = conn.execute(
            "SELECT kind, ticker, close FROM reference_close WHERE ticker = ANY(%s) AND session_day = %s",
            ([ticker, benchmark], since.astimezone(config.ET).date()),
        ).fetchall()
    bars = pd.DataFrame(rows, columns=["ts", "ticker", "close"])
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    bars["close"] = bars["close"].astype(float)
    prior = {t: float(c) for k, t, c in closes if k == "prior"}
    day = {t: float(c) for k, t, c in closes if k == "day"}
    return bars, prior, day


async def live_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> dict:
    since = _parse_ts(since_ts)
    window = int(config.get("REACTION_WINDOW_MIN") or 30)
    bars, prior, day = await asyncio.to_thread(_tiger_query, ticker, benchmark, since, window)
    r = compute_reaction(bars, prior, day, ticker, benchmark, since, window, "Tiger Data · bars_1m continuous aggregate", False)
    return r.model_dump()
