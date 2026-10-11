"""Gemini: chunk transcription + claim extraction, and the fact-check agent.

Model IDs and SDK details follow docs/sdk_notes.md (google-genai 2.29.0,
gemini-3.8-flash): no temperature, thinking_level instead of budgets, and
FunctionResponse parts built by hand with the call id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from . import config, observability
from .claims import METRICS, ChunkResult, Claim
from .models import Direction, Flag, Stance

log = logging.getLogger("calldesk")

_client = None
_client_key: str | None = None


def client():
    """One async-capable client per API key, with timeouts and retries on 408/429/5xx."""
    global _client, _client_key
    from google import genai
    from google.genai import types

    key = config.get("GEMINI_API_KEY")
    if _client is None or key != _client_key:
        _client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=30_000,  # milliseconds
                retry_options=types.HttpRetryOptions(attempts=3, initial_delay=0.5, max_delay=4.0),
            ),
        )
        _client_key = key
    return _client


def _types():
    from google.genai import types

    return types


def _require_output(resp) -> None:
    """Blocked or truncated responses come back with no text; fail loudly instead of returning nothing."""
    if resp.parsed is None and not resp.text:
        cands = getattr(resp, "candidates", None) or []
        reason = cands[0].finish_reason if cands else getattr(resp, "prompt_feedback", None)
        raise RuntimeError(f"empty Gemini response ({reason})")


# ---------------------------------------------------------------- listening
LISTEN_SYSTEM = """You are the listening layer of Call Desk, which monitors earnings calls live for corporate IR and FP&A teams.
You receive one short audio chunk of {company} ({ticker}): {title}, held {date}.
Known participants: Ravi Pamnani (SVP Investor Relations), Ramon Laguarta (Chairman and CEO), Steve Schmitt (EVP and CFO),
the operator, and sell-side analysts (introduced by the operator or greeted by name, e.g. "Hey Dara" means Dara Mohsenian of Morgan Stanley).
Label executives as "Name (CEO)" / "Name (CFO)", analysts as "Name (Firm)" when known, otherwise "Analyst".

1. Transcribe the chunk verbatim (drop "um"/"uh"). Split it into speaker turns with each turn's start time in seconds from the start of THIS chunk.
   Use the previous context only to keep speakers consistent and to finish interpreting sentences; never re-transcribe it.
2. Extract claims that could move the stock or a peer company's model: guidance changes (raised, cut, reaffirmed, or implied:
   "the margin piece that's driving the guide down in EPS" is a CUT to core EPS guidance even though no number is spoken),
   reported KPIs, margins, pricing, volume, tariffs, FX, capex, buybacks, dividends, cost programs, next-year framework,
   macro/consumer commentary, and clear tone shifts. Only extract a claim if its key words are spoken in THIS chunk.
   - quote: the shortest verbatim span that states the claim.
   - metric: exactly one of: {metrics}.
   - direction versus prior guidance: cut | raise | reaffirm; "new" for a newly reported fact or KPI; "n/a" otherwise.
   - new_value: the value exactly as spoken with units and period (e.g. "+3.1% in Q3"); null if no number or level is stated.
   - prior_value: only if the speaker states it; else null.
   - materiality 0-1, judged for a peer company's IR/FP&A team who ALREADY have this morning's press release:
     0.9-1.0 a change to full-year guidance (EPS, revenue, margin), stated or implied ("driving the guide down in EPS");
     0.7-0.89 forward-looking information the release does not spell out: margin trajectory, segment outlook, cost or pricing actions,
     next-year framework or its timing, capital allocation changes;
     0.4-0.59 restating the quarter's reported results (growth rates, volumes, margins for the quarter just reported), tone, color;
     at most 0.4 analyst questions or restatements, pleasantries.
     Group a run of quarterly statistics into at most one claim. Return an empty claims list when nothing material is said."""


async def transcribe_and_extract(wav: bytes, context: list[str], k: int, start: float, end: float, info: config.CallInfo) -> ChunkResult:
    types = _types()
    model = config.get("GEMINI_MODEL_FAST")
    system = LISTEN_SYSTEM.format(company=info.company, ticker=info.ticker, title=info.title, date="October 8, 2026",
                                  metrics=", ".join(METRICS))
    prompt = (f"Chunk {k} covers {start:.0f}s to {end:.0f}s of the clip.\n"
              f"Previous context (already transcribed, do not repeat):\n{chr(10).join(context) or '(start of clip)'}")
    with observability.agent_transaction(
        "invoke_agent call-desk-listener", {"gen_ai.agent.name": "call-desk-listener", "calldesk.chunk": k}, agent="call-desk-listener"
    ):
        resp = await client().aio.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav"), prompt],
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=ChunkResult,
                thinking_config=types.ThinkingConfig(thinking_level="low"),
                # A live feed cannot wait 90 s for one chunk: shorter timeout, fewer retries than the agent.
                http_options=types.HttpOptions(timeout=15_000, retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1.0)),
            ),
        )
    _require_output(resp)
    result = resp.parsed if isinstance(resp.parsed, ChunkResult) else ChunkResult.model_validate_json(resp.text)
    for c in result.claims:
        if c.metric not in METRICS:
            c.metric = "other"
    return result


# ---------------------------------------------------------------- agent
AGENT_SYSTEM = """You are Call Desk's fact-check agent for corporate IR and FP&A teams who track peers' earnings calls.
A claim was just spoken live on {company}'s ({ticker}) Q3 2026 earnings call (October 8, 2026, Q&A only; the press release and
prepared remarks were posted at 6 AM ET). Executives on Q&A calls often refer to guidance without saying the numbers.
Verify the claim fast. In ONE step, call all three tools in parallel:
- search_prior_guidance: find what the company guided for this metric last quarter (Q2 2026, July 2026).
- search_live_web: find this morning's Q3 2026 release or news coverage that states the exact current value for this metric.
- get_price_reaction: ticker {ticker}, since_ts = the claim's call time, benchmark {benchmark}."""

TOOL_DECLS = [
    {
        "name": "search_prior_guidance",
        "description": "Semantic search over the company's prior-quarter earnings transcripts and guidance tables (Snowflake Cortex Search).",
        "parameters_json_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "Natural-language question, e.g. 'What was PepsiCo's core EPS guidance last quarter?'"}}, "required": ["query"]},
    },
    {
        "name": "search_live_web",
        "description": "Live web search plus page extraction (Nimble) for press releases, news and analyst reaction. Returns titles, URLs and relevant passages.",
        "parameters_json_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_price_reaction",
        "description": "Ticker % move after a timestamp versus a benchmark ETF, from 1-minute bars (Tiger Data continuous aggregate). The difference is the abnormal move.",
        "parameters_json_schema": {"type": "object", "properties": {
            "ticker": {"type": "string"}, "since_ts": {"type": "string", "description": "ISO 8601 timestamp of the quote"},
            "benchmark": {"type": "string", "description": "Benchmark ticker, e.g. XLP or SPY"}}, "required": ["ticker", "since_ts", "benchmark"]},
    },
]


class EvidenceRef(BaseModel):
    source: Literal["prior", "web"]
    index: int = Field(description="The number shown in brackets, e.g. 2 for [W2]")
    stance: Stance


class SynthRelated(BaseModel):
    metric_label: str
    prior_value: str | None = None
    new_value: str
    direction: Direction


class Synthesis(BaseModel):
    direction: Direction = Field(description="cut | raise | reaffirm | new | n/a, versus prior guidance")
    new_value: str | None = Field(description="Exact current value with units: as spoken, or from the sources if the speaker gave no number. Null if not found.")
    new_value_source: str = Field(description="'spoken', or a short source name such as 'Q3 2026 release'")
    prior_value: str | None = Field(description="Last quarter's guidance for this metric, as written in the prior-guidance source. Null if not found.")
    prior_found: bool
    consistent_with_prior: bool | None = Field(description="Does the stated change agree with prior vs new values? Null if not comparable.")
    what_changed: str = Field(description="One sentence, at most 30 words, leading with the metric and the numbers.")
    why_it_matters: str = Field(description="One sentence in a CFO's voice for a peer company's IR or FP&A team, at most 30 words.")
    evidence: list[EvidenceRef] = Field(description="Every source you relied on, with its stance toward the claim.")
    related_changes: list[SynthRelated] = Field(default_factory=list, description="Only for full-year guidance claims: the other guidance lines in the same release, current vs previous (max 6).")


SYNTH_SYSTEM = """You are Call Desk's fact-check agent. Write the verdict for one live earnings-call claim using ONLY the numbered sources.
Cite sources by their number. Prior values come from [P#] sources (last quarter); current values come from the call itself or [W#] sources.
Be exact with numbers and units; never invent a number that is not in the claim or a source. Keep CFO tone: plain, specific, no hype.
If the claim is a full-year guidance change and a source shows the full guidance table, fill related_changes with each line (current vs previous).
Core EPS and core constant-currency EPS are DIFFERENT metrics. Report the one named in Metric; put the other in related_changes.
If no number was spoken, you MUST take new_value from a [W#] source that states it for this exact metric. Use 'not stated' only when no source does.
Take prior_value from [P#] sources first. If a prior source says the company steered toward the low end of a range,
write prior_value as 'low end of <range>'."""


def _claim_block(claim: Claim, flag: Flag) -> str:
    return (f"Speaker: {claim.speaker}\nQuote: \"{claim.quote}\"\nCall time: {flag.call_ts}\nMetric: {flag.metric_label} ({claim.metric})\n"
            f"Claim type: {claim.claim_type}\nDirection heard: {claim.direction}\nValue spoken: {claim.new_value or 'none'}\n"
            f"Prior value spoken: {claim.prior_value or 'none'}")


async def agent_fact_check(claim: Claim, flag: Flag, info: config.CallInfo, plan: dict[str, dict],
                           run_tools: Callable[[list[tuple[str, dict]]], Awaitable[dict]]) -> tuple[dict, dict]:
    """Step 1: the model plans parallel tool calls. Step 2: run them. Step 3: structured verdict."""
    types = _types()
    model = config.get("GEMINI_MODEL_AGENT")
    system = AGENT_SYSTEM.format(company=info.company, ticker=info.ticker, benchmark=info.primary_benchmark)
    tool_cfg = types.GenerateContentConfig(
        system_instruction=system,
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**d) for d in TOOL_DECLS])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        thinking_config=types.ThinkingConfig(thinking_level="low"),
    )
    resp = await client().aio.models.generate_content(model=model, contents=[_claim_block(claim, flag)], config=tool_cfg)
    calls = [(c.name, dict(c.args or {})) for c in (resp.function_calls or []) if c.name in plan]
    # Pin the price lookup to the plan (quote time, ticker, benchmark); the model may reformat them.
    # The model only writes the query; the plan's other arguments (metric, company) always apply.
    calls = [(n, plan[n]) if n == "get_price_reaction" else (n, {**plan[n], **{k: v for k, v in a.items() if k in plan[n]}}) for n, a in calls]
    requested = {n for n, _ in calls}
    calls += [(n, args) for n, args in plan.items() if n not in requested]  # every flag gets all three checks
    log.info("agent %s tool calls: %s", flag.id, [(n, a.get("query", a.get("ticker"))) for n, a in calls])
    results = await run_tools(calls)

    prior_res = results["search_prior_guidance"]
    prior_hits = (prior_res.data or {}).get("hits") or [] if prior_res.ok else []
    web_hits = results["search_live_web"].data if results["search_live_web"].ok else []
    price = results["get_price_reaction"]
    sources = ["PRIOR-QUARTER SOURCES (Snowflake Cortex Search):"]
    sources += [f"[P{i + 1}] {h.get('doc_title', '')}: {h.get('snippet', '')}" for i, h in enumerate(prior_hits)] or ["(none: tool failed or empty)"]
    sources += ["", "LIVE WEB SOURCES (Nimble):"]
    sources += [f"[W{i + 1}] {h.get('title', '')} <{h.get('url', '')}>: {h.get('snippet', '')}" for i, h in enumerate(web_hits)] or ["(none: tool failed or empty)"]
    if price.ok and price.data:
        p = price.data
        sources += ["", f"PRICE (Tiger Data): {p['ticker']} {p['pct_move']:+.2f}% vs {p['benchmark']} {p['benchmark_pct_move']:+.2f}% "
                        f"over {p['window']} after the quote; by close {p.get('close_pct_move')}% vs benchmark {p.get('close_benchmark_pct_move')}%."]
    synth = await client().aio.models.generate_content(
        model=model,
        contents=[f"CLAIM\n{_claim_block(claim, flag)}\n\n" + "\n".join(sources)],
        config=types.GenerateContentConfig(
            system_instruction=SYNTH_SYSTEM,
            response_mime_type="application/json",
            response_schema=Synthesis,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    _require_output(synth)
    s = synth.parsed if isinstance(synth.parsed, Synthesis) else Synthesis.model_validate_json(synth.text)
    out = s.model_dump()
    out["consistent"] = out.pop("consistent_with_prior")
    out["evidence"] = [{**e, "index": e["index"] - 1} for e in out["evidence"]]
    return results, out


async def ping() -> str:
    """Cheapest possible live check, used by scripts/check_env.py --ping."""
    resp = await asyncio.wait_for(
        client().aio.models.generate_content(model=config.get("GEMINI_MODEL_FAST"), contents=["Reply with the single word: ok"]), 20
    )
    return (resp.text or "").strip()


__all__ = ["agent_fact_check", "ping", "transcribe_and_extract"]
