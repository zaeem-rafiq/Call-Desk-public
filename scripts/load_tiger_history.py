"""Load ~30 days of real 1-minute bars for ~40 tickers into Tiger Data's price_ticks hypertable.

The call-day seed (scripts/seed_tiger.py) holds about two days for ten tickers, which is
too small to measure columnstore compression or query time at a realistic size. This
script adds a month of history around it:

1. Fetches yfinance 1-minute bars (prepost=True) for the last 30 days, in 7-day windows
   (yfinance serves 1-minute bars only for the last 30 days, at most 7 days per request).
   Tickers that fail are skipped. Bars stay in memory: no price file is written.
2. Turns each bar into four ticks with seed_tiger.bar_ticks() (the seed's own convention)
   and COPYs them in per-week batches. A (ticker, ET day) that already has rows is never
   touched; neither is any ET day that falls in an existing chunk, so the call-day chunks
   (and the app's reaction numbers) stay exactly as they are.
3. Moves each new chunk to the columnstore right after its batch, and refreshes bars_1m
   for the inserted days only.
4. Checks safety: the app's own reaction and peer queries for the PEP and DAL stage
   quotes, plus checksums of the existing raw rows and bars_1m rows, before and after.
   It exits 1 if anything differs.

Usage:
  .venv/bin/python scripts/load_tiger_history.py --dry-run   # fetch and count, write nothing
  .venv/bin/python scripts/load_tiger_history.py
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import config  # noqa: E402
from app.tools import prices  # noqa: E402
from seed_tiger import bar_ticks  # noqa: E402

TICKERS = ("PEP KO MDLZ KDP PG CL KMB GIS HSY MO PM KHC CPB CAG SJM HRL MKC CLX CHD STZ TAP KR WMT COST TGT XLP "
           "DAL UAL AAL LUV ALK JBLU JETS SPY QQQ XLY XLE XLF XLK XLV").split()
WINDOW_DAYS = 7  # yfinance: at most 7 days of 1-minute bars per request
MAX_UNCOMPRESSED_MB = 800
FALLBACK_BYTES_PER_ROW = 100  # heap tuple + ts index entry, used only if the table has no stats yet
# The app's stage quotes: (call id, ticker, benchmark, quote time). DAL's is a flag in data/replay/latest-dal_q3_2026.
CHECKS = (("pep_q3_2026", "PEP", "XLP", "2026-10-08T08:20:18-04:00"),
          ("dal_q3_2026", "DAL", "JETS", "2026-10-09T10:17:32-04:00"))
TIMING_RUNS = 5
ET_DAY = "(ts AT TIME ZONE 'America/New_York')::date"


# ---- fetch ------------------------------------------------------------------
def fetch(ticker: str, start: date, end: date) -> pd.DataFrame:
    import yfinance as yf

    hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat(), interval="1m",
                                     prepost=True, auto_adjust=False)
    if hist.empty:
        return pd.DataFrame()
    hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
    return pd.DataFrame({
        "ts": hist.index.tz_convert("UTC"),
        "ticker": ticker,
        "open": hist["Open"].to_numpy(float),
        "high": hist["High"].to_numpy(float),
        "low": hist["Low"].to_numpy(float),
        "close": hist["Close"].to_numpy(float),
        "volume": hist["Volume"].fillna(0).to_numpy("int64"),
    })


def fetch_all(tickers: list[str], days: int) -> tuple[pd.DataFrame, dict[str, str]]:
    today = datetime.now(config.ET).date()
    first = today - timedelta(days=days - 1)
    windows = []
    start = first
    while start <= today:
        end = min(start + timedelta(days=WINDOW_DAYS), today + timedelta(days=1))
        if any((start + timedelta(days=i)).weekday() < 5 for i in range((end - start).days)):  # skip weekend-only windows
            windows.append((start, end))
        start += timedelta(days=WINDOW_DAYS)
    jobs = [(t, s, e) for t in tickers for s, e in windows]
    errors: dict[str, str] = {}

    def run(job):
        t, s, e = job
        try:
            return t, fetch(t, s, e), None
        except Exception as exc:  # noqa: BLE001 - one ticker's failure never stops the load
            return t, pd.DataFrame(), f"{type(exc).__name__}: {exc}"[:120]

    frames = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, df, err in pool.map(run, jobs):
            if err:
                errors.setdefault(t, err)
            if not df.empty:
                frames.append(df)
    bars = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["ts", "ticker"])
    bars = bars.drop_duplicates(subset=["ticker", "ts"]).sort_values(["ts", "ticker"]).reset_index(drop=True)
    if not bars.empty:
        bars["day"] = bars["ts"].dt.tz_convert(config.ET).dt.date
    failed = {t: errors.get(t, "no bars returned") for t in tickers if bars.empty or t not in set(bars["ticker"])}
    print(f"fetched {len(bars):,} bars for {len(tickers) - len(failed)}/{len(tickers)} tickers, "
          f"{first} .. {today} ET in {len(windows)} windows")
    return bars, failed


# ---- database state -----------------------------------------------------------
def storage(conn) -> dict:
    import psycopg

    stats = None
    for fn in ("hypertable_columnstore_stats", "hypertable_compression_stats"):  # TimescaleDB 2.18+ name first
        try:
            stats = conn.execute(f"SELECT total_chunks, number_compressed_chunks, before_compression_total_bytes, "
                                 f"after_compression_total_bytes FROM {fn}('price_ticks')").fetchone()
            break
        except psycopg.Error:
            continue
    chunks, compressed, before, after = stats or (None, None, None, None)
    return {
        "rows": conn.execute("SELECT count(*) FROM price_ticks").fetchone()[0],
        "chunks": chunks, "compressed_chunks": compressed, "before_bytes": before, "after_bytes": after,
        "ratio": round(before / after, 1) if before and after else None,
        "total_bytes": conn.execute("SELECT hypertable_size('price_ticks')").fetchone()[0],
        "bars_1m_rows": conn.execute("SELECT count(*) FROM bars_1m").fetchone()[0],
    }


def chunk_ranges(conn) -> list[tuple[str, datetime, datetime, bool]]:
    return conn.execute(
        "SELECT format('%I.%I', chunk_schema, chunk_name), range_start, range_end, is_compressed "
        "FROM timescaledb_information.chunks WHERE hypertable_name = 'price_ticks' ORDER BY range_start"
    ).fetchall()


def checksums(conn, days: list[date]) -> dict:
    """Exact fingerprint of the existing raw rows and their bars_1m rows (days that held data before the load)."""
    raw = conn.execute(
        f"SELECT count(*), md5(string_agg(concat_ws('|', ts, ticker, price, volume), ',' ORDER BY ticker, ts, price, volume)) "
        f"FROM price_ticks WHERE {ET_DAY} = ANY(%s)", (days,)).fetchone()
    agg = conn.execute(
        "SELECT count(*), md5(string_agg(concat_ws('|', bucket, ticker, open, high, low, close, volume), ',' "
        "ORDER BY ticker, bucket)) FROM bars_1m WHERE (bucket AT TIME ZONE 'America/New_York')::date = ANY(%s)",
        (days,)).fetchone()
    return {"price_ticks": tuple(raw), "bars_1m": tuple(agg)}


def app_reactions() -> dict:
    """The app's own live fetch (reaction SQL, sparkline bars, reference closes, peers) for each stage quote."""
    out = {}
    for call_id, ticker, benchmark, since in CHECKS:
        params = prices._reaction_params(ticker, benchmark, prices._parse_ts(since))
        peers = tuple(config.call_info(call_id).peers)
        bars, prior, day, numbers, _ms, _storage, peer_rows = prices._tiger_fetch(params, peers)
        out[ticker] = {"since": since, "benchmark": benchmark, "numbers": numbers, "peers": peer_rows,
                       "bars": bars, "prior": prior, "day": day}
    return out


def same_reactions(a: dict, b: dict) -> list[str]:
    diffs = []
    for t in a:
        for key in ("numbers", "peers", "prior", "day"):
            if a[t][key] != b[t][key]:
                diffs.append(f"{t} {key}")
        if not a[t]["bars"].equals(b[t]["bars"]):
            diffs.append(f"{t} sparkline bars")
    return diffs


def timings(conn) -> dict[str, tuple[float, float]]:
    """Median of TIMING_RUNS: server time (EXPLAIN ANALYZE planning + execution) and wall time of REACTION_SQL."""
    out = {}
    for _call_id, ticker, benchmark, since in CHECKS:
        params = prices._reaction_params(ticker, benchmark, prices._parse_ts(since))
        server, wall = [], []
        for _ in range(TIMING_RUNS):
            plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + prices.REACTION_SQL, params).fetchone()[0][0]
            server.append(plan.get("Planning Time", 0) + plan.get("Execution Time", 0))
            started = time.perf_counter()
            conn.execute(prices.REACTION_SQL, params).fetchone()
            wall.append((time.perf_counter() - started) * 1000)
        out[ticker] = (statistics.median(server), statistics.median(wall))
    return out


# ---- load ---------------------------------------------------------------------
def day_window(day: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(day, dtime(0), tzinfo=config.ET)
    return lo, datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=config.ET)


def day_runs(days: list[date]) -> list[tuple[datetime, datetime]]:
    """Consecutive ET days merged into [start, end) refresh windows."""
    runs: list[list[date]] = []
    for d in sorted(days):
        if runs and d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    return [(day_window(r[0])[0], day_window(r[-1])[1]) for r in runs]


def to_columnstore(conn, chunk: str) -> None:
    import psycopg

    try:
        conn.execute(f"CALL convert_to_columnstore('{chunk}', if_not_columnstore => true)")
    except psycopg.Error:
        conn.execute(f"SELECT compress_chunk('{chunk}', if_not_compressed => true)")


def mb(n: float | None) -> str:
    return "n/a" if n is None else f"{n / 1024 ** 2:,.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and count, write nothing")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--tickers", nargs="+", default=TICKERS)
    args = ap.parse_args()

    if not config.get("TIGER_DATABASE_URL"):
        print("TIGER_DATABASE_URL is not set; nothing to load.")
        return 1
    import psycopg
    from psycopg import sql

    bars, failed = fetch_all(args.tickers, min(args.days, 30))
    for t, why in failed.items():
        print(f"  skipped {t}: {why}")
    if bars.empty:
        print("no bars fetched")
        return 1

    with psycopg.connect(config.get("TIGER_DATABASE_URL"), autocommit=True, connect_timeout=15) as conn:
        before = storage(conn)
        chunks_before = chunk_ranges(conn)
        existing = {(t, d) for t, d in conn.execute(f"SELECT DISTINCT ticker, {ET_DAY} FROM price_ticks").fetchall()}
        existing_days = sorted({d for _t, d in existing})

        # Never touch a (ticker, ET day) that has rows, nor any day that falls in an existing chunk.
        span = bars.groupby("day")["ts"].agg(["min", "max"])
        blocked = {d for d, r in span.iterrows()
                   if any(lo < r["max"] + timedelta(seconds=59) and r["min"] < hi for _c, lo, hi, _z in chunks_before)}
        keep = [d not in blocked and (t, d) not in existing for t, d in zip(bars["ticker"], bars["day"])]
        new = bars[keep].reset_index(drop=True)
        print(f"existing data: {len(existing)} (ticker, ET day) pairs on {[str(d) for d in existing_days]}; "
              f"{len(chunks_before)} chunks")
        print(f"skipping {len(bars) - len(new):,} fetched bars on days already in price_ticks chunks: "
              f"{sorted(str(d) for d in blocked)}")

        bytes_per_row = (before["before_bytes"] / before["rows"]) if before["before_bytes"] and before["rows"] \
            else FALLBACK_BYTES_PER_ROW
        est = 4 * len(new) * bytes_per_row
        print(f"to insert: {len(new):,} bars -> {4 * len(new):,} rows; uncompressed estimate {mb(est)} "
              f"at {bytes_per_row:.0f} B/row (measured on the current table)")
        while est > MAX_UNCOMPRESSED_MB * 1024 ** 2:
            drop = sorted(new["ticker"].unique(), key=args.tickers.index)[-1]
            new = new[new["ticker"] != drop].reset_index(drop=True)
            est = 4 * len(new) * bytes_per_row
            print(f"  over {MAX_UNCOMPRESSED_MB} MB: dropped {drop}; estimate now {mb(est)}")
        if new.empty:
            print("nothing new to insert")
            return 0
        new["week"] = new["ts"].dt.tz_convert(config.ET).dt.tz_localize(None).dt.to_period("W").astype(str)
        peak = max(4 * n * bytes_per_row for n in new.groupby("week").size())
        print(f"{new['week'].nunique()} weekly batches; peak uncompressed batch {mb(peak)}")
        if args.dry_run:
            print("dry run: nothing written")
            return 0

        print("\nbefore: app reaction queries, checksums, timings")
        react_before = app_reactions()
        sums_before = checksums(conn, existing_days)
        time_before = timings(conn)

        inserted = 0
        known = {c for c, *_ in chunks_before}
        for week, batch in new.groupby("week", sort=True):
            started = time.perf_counter()
            rows = bar_ticks(batch)
            with conn.cursor() as cur, cur.copy("COPY price_ticks (ts, ticker, price, volume) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            inserted += len(rows)
            fresh = [c for c, *_ in chunk_ranges(conn) if c not in known]
            for chunk in fresh:
                to_columnstore(conn, chunk)
            known.update(fresh)
            for lo, hi in day_runs(sorted(batch["day"].unique())):
                conn.execute(sql.SQL("CALL refresh_continuous_aggregate('bars_1m', {}, {})").format(sql.Literal(lo), sql.Literal(hi)))
            print(f"  week {week}: {len(rows):,} rows, {batch['ticker'].nunique()} tickers, "
                  f"{len(fresh)} chunks to columnstore, {time.perf_counter() - started:.1f} s")

        print("\nafter: app reaction queries, checksums, timings")
        react_after = app_reactions()
        sums_after = checksums(conn, existing_days)
        time_after = timings(conn)
        after = storage(conn)

    loaded = sorted(new["ticker"].unique(), key=args.tickers.index)
    print("\n==== report ====")
    print(f"tickers loaded ({len(loaded)}): {' '.join(loaded)}")
    print(f"tickers skipped ({len(failed)}): {' '.join(failed) or 'none'}")
    print(f"days loaded: {new['day'].min()} .. {new['day'].max()} ET ({new['day'].nunique()} trading days)")
    print(f"rows inserted into price_ticks: {inserted:,}")
    for label, s in (("before", before), ("after", after)):
        print(f"{label:>6}: {s['rows']:,} rows, {s['compressed_chunks']}/{s['chunks']} chunks in columnstore, "
              f"{mb(s['before_bytes'])} -> {mb(s['after_bytes'])} ({s['ratio']}x smaller), "
              f"hypertable_size {mb(s['total_bytes'])}, bars_1m {s['bars_1m_rows']:,} rows")
    for t, r in react_after.items():
        n0, n1 = react_before[t]["numbers"], r["numbers"]
        print(f"{t} vs {r['benchmark']} since {r['since']}: before {n0['pct_move']:+.3f}% vs {n0['benchmark_pct_move']:+.3f}% "
              f"= {n0['vs_benchmark']:+.3f} pp (day {n0['day_vs_benchmark']}, close {n0['close_vs_benchmark']}); "
              f"after {n1['pct_move']:+.3f}% vs {n1['benchmark_pct_move']:+.3f}% = {n1['vs_benchmark']:+.3f} pp "
              f"(day {n1['day_vs_benchmark']}, close {n1['close_vs_benchmark']})")
        print(f"   peers before {[(p['ticker'], p['vs_benchmark']) for p in react_before[t]['peers']]}")
        print(f"   peers after  {[(p['ticker'], p['vs_benchmark']) for p in r['peers']]}")
        (s0, w0), (s1, w1) = time_before[t], time_after[t]
        print(f"   reaction query, median of {TIMING_RUNS}: server {s0:.2f} -> {s1:.2f} ms; wall {w0:.1f} -> {w1:.1f} ms")
    print(f"existing-day checksums before {sums_before}")
    print(f"existing-day checksums after  {sums_after}")
    diffs = same_reactions(react_before, react_after) + [k for k in sums_before if sums_before[k] != sums_after[k]]
    if diffs:
        print(f"MISMATCH after load: {', '.join(diffs)}")
        return 1
    print("app reaction numbers, peers, sparkline bars and existing-day checksums are identical before and after")
    return 0


if __name__ == "__main__":
    sys.exit(main())
