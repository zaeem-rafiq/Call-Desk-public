"""Fetch intraday prices (pre/post market included) for one trading day.

Writes a long-format CSV of bars plus a CSV of each ticker's prior regular-session close.

Example:
    .venv/bin/python scripts/fetch_prices.py --date 2026-10-08 --tickers PEP SPY XLP \
        --out data/prices.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# Finest first. yfinance only serves 1m bars for roughly the last 30 days.
INTERVALS = ["1m", "2m", "5m", "15m", "1h"]
EXCHANGE_TZ = "America/New_York"


def fetch_intraday(ticker: str, day: date) -> pd.DataFrame:
    """Return the finest available bars for `day`, with an `interval` column."""
    for interval in INTERVALS:
        hist = yf.Ticker(ticker).history(
            start=day.isoformat(),
            end=(day + timedelta(days=1)).isoformat(),
            interval=interval,
            prepost=True,
            auto_adjust=False,
        )
        if hist.empty:
            continue
        hist = hist[hist.index.tz_convert(EXCHANGE_TZ).date == day]
        if hist.empty:
            continue
        bars = pd.DataFrame(
            {
                "ts": hist.index.tz_convert("UTC").map(lambda t: t.isoformat()),
                "ticker": ticker,
                "open": hist["Open"].to_numpy(),
                "high": hist["High"].to_numpy(),
                "low": hist["Low"].to_numpy(),
                "close": hist["Close"].to_numpy(),
                "volume": hist["Volume"].astype("int64").to_numpy(),
                "interval": interval,
            }
        )
        return bars
    raise RuntimeError(f"No intraday data for {ticker} on {day} at any of {INTERVALS}")


def fetch_prior_close(ticker: str, day: date) -> dict:
    """Return the last regular-session daily close strictly before `day`."""
    hist = yf.Ticker(ticker).history(
        start=(day - timedelta(days=10)).isoformat(),
        end=day.isoformat(),
        interval="1d",
        auto_adjust=False,
    )
    hist = hist[hist.index.tz_convert(EXCHANGE_TZ).date < day]
    if hist.empty:
        raise RuntimeError(f"No daily data for {ticker} before {day}")
    last = hist.iloc[-1]
    return {
        "ticker": ticker,
        "date": hist.index[-1].tz_convert(EXCHANGE_TZ).date().isoformat(),
        "close": float(last["Close"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--tickers", required=True, nargs="+")
    parser.add_argument("--out", required=True, type=Path, help="Intraday bars CSV")
    parser.add_argument(
        "--prior-out",
        type=Path,
        default=None,
        help="Prior-close CSV (default: prior_close.csv next to --out)",
    )
    args = parser.parse_args(argv)
    prior_out = args.prior_out or args.out.with_name("prior_close.csv")

    bars = pd.concat([fetch_intraday(t, args.date) for t in args.tickers], ignore_index=True)
    prior = pd.DataFrame([fetch_prior_close(t, args.date) for t in args.tickers])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    bars.to_csv(args.out, index=False)
    prior.to_csv(prior_out, index=False)

    for ticker, group in bars.groupby("ticker", sort=False):
        print(f"{ticker}: {len(group)} bars at {group['interval'].iloc[0]} "
              f"({group['ts'].iloc[0]} .. {group['ts'].iloc[-1]})")
    print(f"Wrote {args.out} and {prior_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
