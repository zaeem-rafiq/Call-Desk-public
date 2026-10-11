"""Live self-check: does every sponsor feature work right now? Prints PASS / WARN / FAIL per check.

Usage:
  .venv/bin/python scripts/verify_live.py              full check: 3 claims at once through the whole fact-check
                                                       (verdict + enrichments) with live keys, ~60-90 s
  .venv/bin/python scripts/verify_live.py --only tiger,voice,brief,sentry
                                                       only the named feature self-checks (cheap, ~10-30 s)
Exit status 1 if any check FAILs. WARN = an external service limit (e.g. Nimble 402) the app already handles.
Nimble agent runs are billed: the full check starts one only for a numeric metric with nothing stored in
data/cache/nimble_verify.json (today: none, so 0 runs). Never set NIMBLE_VERIFY_FRESH=1 for this check.
Never prints secrets. Needs keys in .env; mocked integrations are reported as FAIL in the full check.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

ROWS: list[tuple[str, str, str, str]] = []  # (area, check, PASS|WARN|FAIL, detail)

CLAIMS = [
    ("it's the margin piece that's driving the guide down in EPS", "core_eps_growth", "Core EPS growth", "cut", None),
    ("sales, as you mentioned, we expect to be robust", "organic_revenue_growth", "Organic revenue growth", "reaffirm", "robust"),
    ("we're going to be using revenue management tools in the coming months to partially mitigate this margin pressure",
     "operating_margin", "Operating margin", "n/a", None),
]


def row(area: str, check: str, ok: bool | None, detail: str = "") -> None:
    ROWS.append((area, check, "PASS" if ok else ("WARN" if ok is None else "FAIL"), detail))


async def full_check() -> None:
    from app import factcheck
    from app.claims import Claim
    from app.models import Flag

    modes = config.integration_modes()
    for name, mode in modes.items():
        row("setup", f"{name} live", mode == "live", mode)
    info = config.call_info()
    flags, checked_at = [], {}
    started = time.monotonic()

    async def one(quote, metric, label, direction, value):
        claim = Claim(quote=quote, speaker="Steve Schmitt (CFO)", claim_type="guidance", metric=metric, direction=direction,
                      new_value=value, materiality=0.9)
        flag = Flag(id=f"verify-{metric}", call_ts="2026-10-08T08:20:17-04:00", t_offset=147.2, speaker=claim.speaker, quote=quote,
                    claim_type="guidance", metric=metric, metric_label=label, direction=direction, new_value=value)

        def publish(f):
            if f.status == "checked":
                checked_at.setdefault(f.id, time.monotonic() - started)
        flags.append(await factcheck.fact_check(claim, flag, info, publish))

    await asyncio.gather(*(one(*c) for c in CLAIMS))
    total = time.monotonic() - started
    by = {f.metric: f for f in flags}
    eps = by["core_eps_growth"]

    # Gemini: the headline card
    new, prior = eps.new_value or "", eps.prior_value or ""
    row("gemini", "EPS verdict numbers", "2.5" in new and "3.5" in new and "5" in prior and "7" in prior and eps.direction == "cut",
        f"{prior} -> {new} ({eps.direction})")
    row("gemini", "no degraded verdicts", not any(f.degraded for f in flags), ", ".join(f.metric for f in flags if f.degraded))
    row("gemini", "verdicts within 45 s", max(checked_at.values(), default=99) < 45,
        ", ".join(f"{k.removeprefix('verify-')} {v:.0f}s" for k, v in checked_at.items()))

    # Snowflake
    row("snowflake", "EPS guidance history (SQL)", len(eps.guidance_history) >= 4, f"{len(eps.guidance_history)} rows")
    for f in flags:
        sa = f.snowflake_agent
        row("snowflake", f"Cortex Agent · {f.metric}", bool(sa and sa.status == "done"),
            f"{sa.status} {sa.verdict} {sa.ms} ms {sa.error or ''}".strip() if sa else "missing")
    org = by["organic_revenue_growth"].snowflake_agent
    if org and org.status == "done":
        row("snowflake", "agent verdict on in-range reaffirm", org.verdict in ("CONSISTENT", "NEW"), org.verdict)

    # Nimble web desk
    for f in flags:
        desk = f.web_desk
        if not desk:
            row("nimble", f"web desk · {f.metric}", False, "missing")
            continue
        for s in desk.steps:
            if s.status == "ok":
                continue
            limited = s.status == "skipped"  # dropped for time, a service limit, or nothing to do: the app handles it
            row("nimble", f"{s.id} · {f.metric}", None if limited else False, f"{s.status}: {s.summary[:90]}")
        ran = [s for s in desk.steps if s.status != "skipped"]
        failed = sum(s.status != "ok" for s in ran)
        row("nimble", f"specialists · {f.metric}", failed <= 1,
            f"{len(ran) - failed}/{len(ran)} ran ok, {len(desk.steps) - len(ran)} skipped")
    primary = next((s for s in eps.web_desk.steps if s.id == "primary"), None) if eps.web_desk else None
    row("nimble", "EPS primary source has the guidance table", bool(primary and primary.status == "ok" and "guidance table" in primary.summary),
        primary.summary if primary else "missing")

    # Tiger Data
    pr = eps.price_reaction
    row("tiger", "price reaction from Tiger", bool(pr and not pr.mock and "Tiger" in pr.source), pr.source if pr else "missing")

    # Sentry
    row("sentry", "trace link on every flag", all(f.sentry_trace_url for f in flags), f"{sum(bool(f.sentry_trace_url) for f in flags)}/{len(flags)}")
    row("timing", "whole check incl. enrichments", total < 90, f"{total:.0f}s")


async def feature_checks(only: set[str]) -> None:
    modules = {"tiger": "app.tools.prices", "voice": "app.voice", "brief": "app.briefing", "sentry": "app.observability"}
    for name, mod in modules.items():
        if only and name not in only:
            continue
        fn = getattr(importlib.import_module(mod), "self_check", None)
        if fn is None:
            row(name, "self_check()", None, "not provided yet")
            continue
        try:
            results = await asyncio.wait_for(fn(), timeout=120)
        except Exception as exc:  # noqa: BLE001
            row(name, "self_check()", False, f"{type(exc).__name__}: {exc}"[:120])
            continue
        if not results:
            row(name, "self_check()", None, "returned no checks")
        for check, ok, detail in results:
            row(name, check, ok, detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="comma list of feature self-checks: tiger,voice,brief,sentry")
    args = parser.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    async def everything():  # one event loop: cached async clients (Gemini) are bound to the loop that made them
        await full_check()
        await feature_checks(set())
    asyncio.run(feature_checks(only) if only else everything())
    width = max(len(r[1]) for r in ROWS) if ROWS else 10
    for area, check, status, detail in ROWS:
        print(f"{status:<5} {area:<9} {check:<{width}}  {detail}")
    fails = sum(r[2] == "FAIL" for r in ROWS)
    warns = sum(r[2] == "WARN" for r in ROWS)
    print(f"\n{'FAIL' if fails else 'PASS'}: {len(ROWS) - fails - warns} pass, {warns} warn, {fails} fail")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
