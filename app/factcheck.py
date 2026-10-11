"""Fact-check one claim: run the three tools, synthesize, score confidence.

With Gemini live, the agent decides the tool calls (function calling) and the
orchestrator runs them in parallel; any tool the model skipped is still run so
every flag gets all three checks. Without Gemini, a deterministic plan runs the
same tools and a template writes the text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

from . import confidence, config, observability, tools
from .claims import Claim, numeric_direction, parse_range
from .models import Evidence, Flag, PriceReaction

log = logging.getLogger("calldesk")
MOCKS = Path(__file__).resolve().parent / "mocks"


def default_queries(claim: Claim, label: str, info: config.CallInfo) -> dict[str, dict]:
    return {
        "search_prior_guidance": {"query": f"{info.company} fiscal 2026 {label} guidance outlook"},
        "search_live_web": {"query": f"{info.company} Q3 2026 {label} {claim.new_value or ''} guidance".strip()},
        "get_price_reaction": {"ticker": info.ticker, "since_ts": "", "benchmark": info.primary_benchmark},
    }


async def run_tools(calls: list[tuple[str, dict]]) -> dict[str, tools.ToolResult]:
    """Run tool calls concurrently. Last call wins if a tool is requested twice."""
    results = await asyncio.gather(*(tools.TOOLS[name](**args) for name, args in calls))
    return {name: res for (name, _), res in zip(calls, results)}


def _mock_synthesis(claim: Claim, label: str, prior_hits: list[dict], web_hits: list[dict]) -> dict:
    canned = json.loads((MOCKS / "synthesis.json").read_text()) if (MOCKS / "synthesis.json").exists() else {}
    c = canned.get(claim.metric, {})
    prior_value = claim.prior_value or c.get("prior_value")
    nums = [f"{x:g}" for x in (parse_range(claim.new_value) or ())]

    def stance(snippet: str) -> str:
        return "supports" if nums and all(n in snippet for n in nums) else "neutral"

    return {
        "direction": claim.direction,
        "prior_value": prior_value,
        "prior_found": bool(prior_value and prior_hits),
        "what_changed": c.get("what_changed") or f"{label}: {prior_value or 'n/a'} → {claim.new_value or 'n/a'}",
        "why_it_matters": c.get("why_it_matters") or f"{label} moved to {claim.new_value}; peers' models need to reflect it.",
        "prior_evidence": [{"index": i, "stance": "supports" if prior_value else "neutral"} for i in range(min(2, len(prior_hits)))],
        "web_evidence": [{"index": i, "stance": stance(h.get("snippet", ""))} for i, h in enumerate(web_hits[:4])],
    }


async def fact_check(claim: Claim, flag: Flag, info: config.CallInfo, publish: Callable[[Flag], None]) -> Flag:
    from . import gemini  # local import: gemini imports google-genai lazily

    with observability.agent_transaction(
        f"fact-check {flag.metric_label}",
        {"calldesk.flag_id": flag.id, "calldesk.quote": claim.quote, "calldesk.metric": claim.metric},
    ) as (trace_id, _txn):
        flag.sentry_trace_id = trace_id
        flag.sentry_trace_url = observability.trace_url(trace_id)
        publish(flag)

        plan = default_queries(claim, flag.metric_label, info)
        plan["get_price_reaction"]["since_ts"] = flag.call_ts
        use_llm = not config.is_mock("gemini")
        try:
            if use_llm:
                results, synthesis = await gemini.agent_fact_check(claim, flag, info, plan, run_tools)
            else:
                results = await run_tools(list(plan.items()))
                synthesis = None
        except Exception as exc:  # noqa: BLE001 - agent failure degrades to the deterministic plan
            observability.capture(exc)
            log.warning("agent failed, using deterministic plan: %s", exc)
            results, synthesis = await run_tools(list(plan.items())), None

        prior_res, web_res, price_res = (results.get(k) for k in ("search_prior_guidance", "search_live_web", "get_price_reaction"))
        prior_hits = prior_res.data if prior_res and prior_res.ok else []
        web_hits = web_res.data if web_res and web_res.ok else []
        if synthesis is None:
            synthesis = _mock_synthesis(claim, flag.metric_label, prior_hits, web_hits)

        flag.tool_calls = [r.call for r in results.values()]
        flag.mocked = sorted({c.backend for c in flag.tool_calls if c.mock} | ({"gemini"} if not use_llm else set()))
        _apply_synthesis(flag, claim, synthesis, prior_hits, web_hits, prior_res, web_res)
        _apply_price(flag, price_res)
        _apply_confidence(flag, prior_res, web_res, price_res, synthesis)
        flag.status = "checked"
        publish(flag)
        return flag


def _apply_synthesis(flag, claim, s, prior_hits, web_hits, prior_res, web_res) -> None:
    direction = s.get("direction") or claim.direction
    prior_value = s.get("prior_value") or claim.prior_value
    numeric = numeric_direction(prior_value, claim.new_value)
    if numeric and direction in ("cut", "raise", "reaffirm") and numeric != direction:
        log.info("direction %s overridden by numbers -> %s (%s vs %s)", direction, numeric, prior_value, claim.new_value)
        direction = numeric
    flag.direction = direction
    flag.prior_value = prior_value
    flag.what_changed = s.get("what_changed", "")
    flag.why_it_matters = s.get("why_it_matters", "")
    ev: list[Evidence] = []
    for item in s.get("prior_evidence", []):
        if 0 <= item.get("index", -1) < len(prior_hits):
            h = prior_hits[item["index"]]
            ev.append(Evidence(source=f"Snowflake Cortex Search · {h.get('doc_title', 'prior quarter')}", ref=h.get("source_url") or h.get("doc_title", ""),
                               snippet=h.get("snippet", "")[:400], stance=item.get("stance", "neutral"), mock=prior_res.call.mock))
    for item in s.get("web_evidence", []):
        if 0 <= item.get("index", -1) < len(web_hits):
            h = web_hits[item["index"]]
            ev.append(Evidence(source=f"Nimble · {h.get('source') or h.get('domain') or 'web'}", ref=h.get("url", ""),
                               snippet=h.get("snippet", "")[:400], stance=item.get("stance", "neutral"), mock=web_res.call.mock))
    flag.evidence = ev


def _apply_price(flag, price_res) -> None:
    if price_res and price_res.ok and price_res.data:
        pr = PriceReaction(**price_res.data)
        flag.price_reaction = pr
        flag.evidence.append(Evidence(
            source="Tiger Data · 1-min bars", ref=f"{pr.ticker} vs {pr.benchmark} {pr.window}",
            snippet=f"{pr.ticker} {pr.pct_move:+.2f}% vs {pr.benchmark} {pr.benchmark_pct_move:+.2f}% → abnormal {pr.vs_benchmark:+.2f}pp"
            + (f"; day {pr.day_pct_move:+.2f}% ({pr.day_vs_benchmark:+.2f}pp vs {pr.benchmark})" if pr.day_pct_move is not None else ""),
            stance="neutral", mock=pr.mock,
        ))


def _apply_confidence(flag, prior_res, web_res, price_res, s) -> None:
    prior_ok = bool(prior_res and prior_res.ok)
    consistent = None
    numeric = numeric_direction(flag.prior_value, flag.new_value)
    if numeric:
        consistent = numeric == flag.direction
    if s.get("consistent") is False:
        consistent = False
    web_ok = bool(web_res and web_res.ok)
    web_ev = [e for e in flag.evidence if e.source.startswith("Nimble")]
    sup = sum(e.stance == "supports" for e in web_ev)
    con = sum(e.stance == "contradicts" for e in web_ev)
    total = len(web_res.data) if web_ok and web_res.data else 0
    abnormal = flag.price_reaction.vs_benchmark if flag.price_reaction else None
    flag.confidence, flag.confidence_components, flag.confidence_rationale = confidence.combine(
        confidence.prior_score(prior_ok, bool(s.get("prior_found")), consistent),
        confidence.web_score(web_ok, sup, con, total),
        confidence.price_score(bool(price_res and price_res.ok), abnormal),
    )
