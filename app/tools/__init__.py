"""The fact-check agent's three tools. Each returns a ToolResult and never raises."""

from __future__ import annotations

from . import live_web, prices, prior_guidance
from .base import ToolResult, run_tool


async def search_prior_guidance(query: str, metric: str = "", ticker: str = "PEP") -> ToolResult:
    """Last quarter's guidance (Snowflake Cortex Search) plus the metric's guidance history (SQL). Data: {hits, history}."""
    return await run_tool(
        "search_prior_guidance", "snowflake", {"query": query, "metric": metric, "ticker": ticker},
        prior_guidance.live_search_prior_guidance, prior_guidance.mock_search_prior_guidance, timeout_s=8,
    )


async def search_live_web(query: str, metric_label: str = "", company: str = "PepsiCo") -> ToolResult:
    """Search the live web for news, the press release, and analyst reaction (Nimble)."""
    return await run_tool(
        "search_live_web", "nimble", {"query": query, "metric_label": metric_label, "company": company},
        live_web.live_search_live_web, live_web.mock_search_live_web, timeout_s=8,
    )


async def get_price_reaction(ticker: str, since_ts: str, benchmark: str = "XLP") -> ToolResult:
    """Ticker move since the quote versus a benchmark (Tiger Data continuous aggregate)."""
    return await run_tool(
        "get_price_reaction", "tiger", {"ticker": ticker, "since_ts": since_ts, "benchmark": benchmark},
        prices.live_price_reaction, prices.mock_price_reaction, timeout_s=12,
    )


TOOLS = {
    "search_prior_guidance": search_prior_guidance,
    "search_live_web": search_live_web,
    "get_price_reaction": get_price_reaction,
}

__all__ = ["TOOLS", "ToolResult", "get_price_reaction", "search_live_web", "search_prior_guidance"]
