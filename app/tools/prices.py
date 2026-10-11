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
MAX_POINTS = 90


def _parse_ts(value: str | datetime) -> datetime:
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=config.ET)


def compute_reaction(
    bars: pd.DataFrame,
    prior_close: dict[str, float],
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

    ref_t = prior_close.get(ticker) or px[ticker].iloc[0]
    ref_b = prior_close.get(benchmark) or px[benchmark].iloc[0]
    window = px[(px.index >= since - SERIES_LOOKBACK) & (px.index <= until)]
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
        series=series,
        source=source,
        mock=mock,
    )


# ---- mock: local CSV ----------------------------------------------------
@lru_cache(maxsize=1)
def _csv_bars() -> tuple[pd.DataFrame, dict[str, float]]:
    bars = pd.read_csv(config.DATA / "prices.csv")
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    prior: dict[str, float] = {}
    prior_path = config.DATA / "prior_close.csv"
    if prior_path.exists():
        pc = pd.read_csv(prior_path)
        prior = dict(zip(pc["ticker"], pc["close"].astype(float)))
    return bars[["ts", "ticker", "close"]], prior


async def mock_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> dict:
    bars, prior = _csv_bars()
    window = int(config.get("REACTION_WINDOW_MIN") or 30)
    r = compute_reaction(bars, prior, ticker, benchmark, _parse_ts(since_ts), window, "local prices.csv (Tiger offline)", True)
    return r.model_dump()


# ---- live: Tiger Data ---------------------------------------------------
def _tiger_query(ticker: str, benchmark: str, since: datetime, window_min: int) -> tuple[pd.DataFrame, dict[str, float]]:
    import psycopg

    start = since - SERIES_LOOKBACK
    end = since + timedelta(minutes=window_min)
    with psycopg.connect(config.get("TIGER_DATABASE_URL"), connect_timeout=8) as conn:
        rows = conn.execute(
            "SELECT bucket, ticker, close FROM bars_1m WHERE ticker = ANY(%s) AND bucket BETWEEN %s AND %s ORDER BY bucket",
            ([ticker, benchmark], start, end),
        ).fetchall()
        prior = dict(conn.execute("SELECT ticker, close FROM prior_close WHERE ticker = ANY(%s)", ([ticker, benchmark],)).fetchall())
    bars = pd.DataFrame(rows, columns=["ts", "ticker", "close"])
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    bars["close"] = bars["close"].astype(float)
    return bars, {k: float(v) for k, v in prior.items()}


async def live_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> dict:
    since = _parse_ts(since_ts)
    window = int(config.get("REACTION_WINDOW_MIN") or 30)
    bars, prior = await asyncio.to_thread(_tiger_query, ticker, benchmark, since, window)
    r = compute_reaction(bars, prior, ticker, benchmark, since, window, "Tiger Data · bars_1m continuous aggregate", False)
    return r.model_dump()
