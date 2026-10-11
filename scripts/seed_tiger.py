"""Seed Tiger Data (TimescaleDB): price-tick hypertable, 1-minute continuous aggregate, reference closes.

yfinance only serves 1-minute bars, so each bar is loaded as four bar-derived ticks
(open at :00, high :20, low :40, close :59). The continuous aggregate rebuilds exact
OHLC bars with first()/max()/min()/last(); a live tick feed would insert real trades
into the same hypertable.

The pipeline runs on the call's own clock (clip offset + call start), so prices line
up with the audio without shifting timestamps.

Usage: .venv/bin/python scripts/seed_tiger.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

HYPERTABLE_NEW = """
CREATE TABLE IF NOT EXISTS price_ticks (
    ts      TIMESTAMPTZ      NOT NULL,
    ticker  TEXT             NOT NULL,
    price   DOUBLE PRECISION NOT NULL,
    volume  BIGINT           NOT NULL DEFAULT 0
) WITH (tsdb.hypertable, tsdb.partition_column = 'ts', tsdb.chunk_interval = '1 day',
        tsdb.enable_columnstore = true, tsdb.segmentby = 'ticker', tsdb.orderby = 'ts DESC')
"""
HYPERTABLE_OLD = [
    """CREATE TABLE IF NOT EXISTS price_ticks (
        ts TIMESTAMPTZ NOT NULL, ticker TEXT NOT NULL, price DOUBLE PRECISION NOT NULL, volume BIGINT NOT NULL DEFAULT 0)""",
    "SELECT create_hypertable('price_ticks', by_range('ts', INTERVAL '1 day'), if_not_exists => TRUE)",
]
CAGG = """
CREATE MATERIALIZED VIEW bars_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket,
       ticker,
       first(price, ts) AS open,
       max(price)       AS high,
       min(price)       AS low,
       last(price, ts)  AS close,
       sum(volume)      AS volume
FROM price_ticks
GROUP BY bucket, ticker
WITH NO DATA
"""
REFERENCE = """
CREATE TABLE IF NOT EXISTS reference_close (
    session_day DATE NOT NULL,
    ticker      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('prior', 'day')),
    close       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (session_day, ticker, kind)
)
"""


def bar_ticks(bars: pd.DataFrame) -> list[tuple]:
    rows = []
    for b in bars.itertuples(index=False):
        t0 = pd.Timestamp(b.ts).to_pydatetime()
        rows += [
            (t0, b.ticker, float(b.open), 0),
            (t0 + timedelta(seconds=20), b.ticker, float(b.high), 0),
            (t0 + timedelta(seconds=40), b.ticker, float(b.low), 0),
            (t0 + timedelta(seconds=59), b.ticker, float(b.close), int(b.volume)),
        ]
    return rows


def main() -> int:
    import psycopg

    url = config.get("TIGER_DATABASE_URL")
    if not url:
        print("TIGER_DATABASE_URL is not set; Tiger stays in mock mode.")
        return 1
    calls = [config.call_info(c["id"]) for c in config.list_calls()]
    calls = [c for c in calls if c.prices_path.exists()]
    if not calls:
        print("no price files found (run scripts/fetch_prices.py)")
        return 1

    with psycopg.connect(url, autocommit=True, connect_timeout=15) as conn:
        version = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        print(f"TimescaleDB {version[0] if version else 'not installed'}")
        try:
            conn.execute(HYPERTABLE_NEW)
        except psycopg.Error as exc:
            print(f"new hypertable syntax unavailable ({exc.diag.message_primary}); using create_hypertable()")
            for stmt in HYPERTABLE_OLD:
                conn.execute(stmt)
        if not conn.execute("SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = 'bars_1m'").fetchone():
            conn.execute(CAGG)
            print("created continuous aggregate bars_1m")
        conn.execute("CREATE INDEX IF NOT EXISTS bars_1m_ticker_bucket ON bars_1m (ticker, bucket DESC)")
        conn.execute(REFERENCE)

        loaded = []
        for info in calls:
            bars = pd.read_csv(info.prices_path)
            ticks = bar_ticks(bars)
            tickers = sorted(bars["ticker"].unique().tolist())
            lo, hi = min(t[0] for t in ticks), max(t[0] for t in ticks)
            conn.execute("DELETE FROM price_ticks WHERE ticker = ANY(%s) AND ts >= %s AND ts <= %s", (tickers, lo, hi))
            with conn.cursor() as cur, cur.copy("COPY price_ticks (ts, ticker, price, volume) FROM STDIN") as copy:
                for row in ticks:
                    copy.write_row(row)
            for kind, path in (("prior", info.prior_close_path), ("day", info.day_close_path)):
                for r in pd.read_csv(path).itertuples(index=False):
                    conn.execute(
                        "INSERT INTO reference_close (session_day, ticker, kind, close) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (session_day, ticker, kind) DO UPDATE SET close = EXCLUDED.close",
                        (info.session_day, r.ticker, kind, float(r.close)),
                    )
            loaded.append((info, tickers, lo, hi, len(ticks)))
        conn.execute("CALL refresh_continuous_aggregate('bars_1m', NULL, NULL)")
        # Historical demo data is immutable: move it to the columnstore now instead of waiting for the policy.
        for (chunk,) in conn.execute("SELECT c::text FROM show_chunks('price_ticks') c").fetchall():
            try:
                conn.execute(f"CALL convert_to_columnstore('{chunk}', if_not_columnstore => true)")
            except psycopg.Error:
                conn.execute(f"SELECT compress_chunk('{chunk}', if_not_compressed => true)")
        stats = conn.execute(
            "SELECT before_compression_total_bytes, after_compression_total_bytes FROM hypertable_compression_stats('price_ticks')"
        ).fetchone()
        if stats and stats[0] and stats[1]:
            print(f"columnstore: {stats[0] // 1024} kB -> {stats[1] // 1024} kB ({stats[0] / stats[1]:.1f}x smaller)")
        try:
            # Live ticks newer than end_offset are still served by real-time aggregation (materialized_only = false).
            conn.execute("SELECT add_continuous_aggregate_policy('bars_1m', start_offset => NULL, "
                         "end_offset => INTERVAL '5 minutes', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE)")
        except psycopg.Error as exc:
            print(f"refresh policy not added: {exc.diag.message_primary}")

        for info, tickers, lo, hi, n in loaded:
            n_bars = conn.execute("SELECT ticker, count(*) FROM bars_1m WHERE ticker = ANY(%s) AND bucket >= %s AND bucket <= %s "
                                  "GROUP BY ticker ORDER BY ticker", (tickers, lo, hi)).fetchall()
            check = conn.execute("SELECT close FROM bars_1m WHERE ticker = %s AND bucket <= %s ORDER BY bucket DESC LIMIT 1",
                                 (info.ticker, info.clip_start)).fetchone()
            print(f"{info.call_id}: {n} ticks for {info.session_day}; bars_1m rows {dict(n_bars)}; "
                  f"{info.ticker} at clip start ({info.clip_start:%H:%M} ET): {check[0] if check else 'n/a'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
