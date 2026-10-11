"""Report which integrations are live and which are mocked. Never prints secret values.

Usage: .venv/bin/python scripts/check_env.py [--ping]
  --ping  also makes one cheap real call to each live integration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

UNBLOCKS = {
    "gemini": "transcription, claim extraction, fact-check agent",
    "nimble": "live web evidence (press release, news)",
    "tiger": "price reaction from the hypertable + continuous aggregate",
    "snowflake": "prior-quarter guidance via Cortex Search",
    "sentry": "error monitoring + AI agent traces",
}


async def ping(name: str) -> str:
    try:
        if name == "gemini":
            from app import gemini

            return f"ok ({config.get('GEMINI_MODEL_FAST')} replied {await gemini.ping()!r})"
        if name == "nimble":
            from app.tools.live_web import live_search_live_web

            r = await live_search_live_web("PepsiCo Q3 2026 earnings guidance", limit=2)
            return f"ok ({len(r)} results)"
        if name == "tiger":
            from app.tools.prices import live_price_reaction

            r = await live_price_reaction("PEP", config.call_info().clip_start.isoformat(), "XLP")
            return f"ok (PEP {r['pct_move']:+.2f}% vs XLP {r['benchmark_pct_move']:+.2f}%)"
        if name == "snowflake":
            from app.tools.prior_guidance import live_search_prior_guidance

            r = (await live_search_prior_guidance("PepsiCo core EPS guidance", metric="core_eps_growth", limit=1))["hits"]
            return f"ok ({r[0]['snippet'][:60]!r})" if r else "connected, but no results (run scripts/seed_snowflake.py)"
        if name == "sentry":
            return "configured (send a test event: GET /api/debug/sentry)"
    except Exception as exc:  # noqa: BLE001
        return f"FAILED: {type(exc).__name__}: {str(exc)[:160]}"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ping", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    state_path = config.DATA / "seed_state.json"
    seeded = json.loads(state_path.read_text()) if state_path.exists() else {}
    report = {}
    for name in config.INTEGRATIONS:
        reason = config.mock_reason(name)
        report[name] = {"mode": "mock" if reason else "live", "reason": reason or "", "seeded": seeded.get(name, {}).get("ok")}
        if args.ping and not reason:
            report[name]["ping"] = asyncio.run(ping(name))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for name, r in report.items():
        line = f"{name:<10} {r['mode'].upper():<5} {UNBLOCKS[name]}"
        if r["reason"]:
            line += f"  [{r['reason']}]"
        if name in ("tiger", "snowflake") and r["mode"] == "live":
            line += "  seeded" if r["seeded"] else "  NOT SEEDED"
        if r.get("ping"):
            line += f"\n{'':<17}ping: {r['ping']}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
