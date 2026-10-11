"""Guidance history for one metric: every time the company stated it before this call.

Live: SQL over CALLDESK.PRIOR.GUIDANCE_LINES in Snowflake (seeded from data/prior/guidance_lines.csv).
Mock: the same rows read from the CSV. Only pre-call guidance is stored (Dec 2025 to Jul 2026).
"""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache

import pandas as pd

from .. import config

CSV = config.DATA / "prior" / "guidance_lines.csv"
COLUMNS = ["AS_OF_DATE", "AS_OF_QUARTER", "AS_OF_EVENT", "METRIC", "METRIC_LABEL", "LOW", "HIGH", "UNIT", "VALUE_TEXT",
           "BASIS", "STATUS", "QUALIFIER", "SOURCE_URL"]
SQL = f"SELECT {', '.join(COLUMNS)} FROM GUIDANCE_LINES WHERE TICKER = %s AND METRIC = %s ORDER BY AS_OF_DATE"


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN -> None


def shape(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = {k.upper(): v for k, v in r.items()}
        out.append({
            "as_of_date": str(r.get("AS_OF_DATE") or "")[:10],
            "as_of_quarter": r.get("AS_OF_QUARTER") or "",
            "event": r.get("AS_OF_EVENT") or "",
            "metric": r.get("METRIC") or "",
            "metric_label": r.get("METRIC_LABEL") or "",
            "low": _num(r.get("LOW")),
            "high": _num(r.get("HIGH")),
            "unit": r.get("UNIT") or "",
            "value_text": r.get("VALUE_TEXT") or "",
            "basis": r.get("BASIS") or "",
            "status": r.get("STATUS") or "",
            "qualifier": r.get("QUALIFIER") if isinstance(r.get("QUALIFIER"), str) else "",
            "source_url": r.get("SOURCE_URL") or "",
        })
    return out


def csv_paths() -> list:
    """guidance_lines.csv from every call's prior/ folder (data/prior for PepsiCo, data/calls/<id>/prior for others)."""
    from .prior_guidance import prior_sources

    return [d / "guidance_lines.csv" for d, _, _ in prior_sources() if (d / "guidance_lines.csv").exists()]


@lru_cache(maxsize=1)
def _csv_rows() -> pd.DataFrame:
    frames = [pd.read_csv(p, dtype=str, keep_default_na=False) for p in csv_paths()] or [pd.read_csv(CSV, dtype=str, keep_default_na=False)]
    return pd.concat(frames, ignore_index=True)


# Listener slug -> other names a company's guidance table uses for it (period prefixes like q4_ are added below).
ALIASES = {"leverage": ("gross_leverage",), "unit_cost_growth": ("nonfuel_unit_cost_growth",), "capacity": ("capacity_growth",)}


def resolve_metric(ticker: str, metric: str, period: str | None = None) -> str:
    """The METRIC value in GUIDANCE_LINES for a listener slug and the claim's period.

    'eps' + 'Q4 2026' -> 'q4_eps'; 'leverage' -> 'gross_leverage'; 'core_eps_growth' (PepsiCo) stays as is.
    A quarter-specific line is preferred when the claim names that quarter; otherwise the full-year line.
    """
    known = set(_csv_rows().loc[lambda d: d["TICKER"] == ticker, "METRIC"])
    names = (metric, *ALIASES.get(metric, ()))
    q = re.search(r"\bq([1-4])\b", (period or "").lower())
    candidates = [f"q{q.group(1)}_{n}" for n in names] if q else []
    candidates += list(names)
    return next((c for c in candidates if c in known), metric)


async def mock_guidance_history(ticker: str, metric: str, period: str | None = None) -> list[dict]:
    df = _csv_rows()
    metric = resolve_metric(ticker, metric, period)
    rows = df[(df["TICKER"] == ticker) & (df["METRIC"] == metric)].sort_values("AS_OF_DATE")
    return shape(rows.to_dict("records"))


async def live_guidance_history(ticker: str, metric: str, period: str | None = None) -> list[dict]:
    from .prior_guidance import query_shared

    rows = await asyncio.to_thread(query_shared, SQL, (ticker, resolve_metric(ticker, metric, period)), "all")
    return shape([dict(zip(COLUMNS, r)) for r in rows])


def summary_line(history: list[dict]) -> str:
    """One line for the verdict prompt, oldest first: 'Dec 8, 2025 approximately +5% to +7% (first given); ...'."""
    parts = []
    for h in history:
        note = f"; {h['qualifier']}" if h["qualifier"] else ""
        parts.append(f"{h['as_of_date']} {h['event']}: {h['value_text']} ({h['basis']}, {h['status']}{note})")
    return " | ".join(parts)
