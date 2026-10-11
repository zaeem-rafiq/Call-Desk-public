"""Transparent confidence score: a weighted sum of three evidence checks.

  prior (0.45)  Snowflake: prior guidance found and the stated change is consistent with it
  web   (0.35)  Nimble: independent web sources corroborate the new value
  price (0.20)  Tiger Data: the market shows a measurable abnormal move vs the benchmark

A failed tool scores 0 for its component, so failures lower confidence instead
of crashing the feed. The rationale string shows every component.
"""

from __future__ import annotations

from .models import ConfidenceComponents

WEIGHTS = {"prior": 0.45, "web": 0.35, "price": 0.20}


def prior_score(tool_ok: bool, prior_found: bool, consistent: bool | None) -> tuple[float, str]:
    if not tool_ok:
        return 0.0, "prior-guidance search failed"
    if not prior_found:
        return 0.3, "no prior guidance found for this metric"
    if consistent is False:
        return 0.2, "claim conflicts with prior guidance"
    if consistent is None:
        return 0.7, "prior guidance found; change not numerically comparable"
    return 1.0, "prior guidance found and the change checks out numerically"


def web_score(tool_ok: bool, supports: int, contradicts: int, total: int) -> tuple[float, str]:
    if not tool_ok:
        return 0.0, "web search failed"
    if contradicts and not supports:
        return 0.0, f"{contradicts} web source(s) contradict"
    if supports >= 2:
        return 1.0 if not contradicts else 0.7, f"{supports} web sources corroborate" + (f", {contradicts} contradict" if contradicts else "")
    if supports == 1:
        return 0.7, "1 web source corroborates"
    return (0.3, f"{total} web results, none corroborate") if total else (0.2, "no web results yet")


def price_score(tool_ok: bool, abnormal_pp: float | None) -> tuple[float, str]:
    if not tool_ok or abnormal_pp is None:
        return 0.0, "price data unavailable"
    a = abs(abnormal_pp)
    if a >= 1.0:
        return 1.0, f"abnormal move {abnormal_pp:+.2f}pp vs benchmark"
    if a >= 0.3:
        return 0.6, f"modest abnormal move {abnormal_pp:+.2f}pp"
    return 0.3, f"little abnormal move ({abnormal_pp:+.2f}pp)"


def combine(prior: tuple[float, str], web: tuple[float, str], price: tuple[float, str]) -> tuple[float, ConfidenceComponents, str]:
    comps = ConfidenceComponents(prior=prior[0], web=web[0], price=price[0])
    total = sum(WEIGHTS[k] * getattr(comps, k) for k in WEIGHTS)
    rationale = " + ".join(
        f"{k} {WEIGHTS[k]:.2f}×{getattr(comps, k):.1f} ({reason})"
        for k, reason in (("prior", prior[1]), ("web", web[1]), ("price", price[1]))
    )
    return round(total, 2), comps, f"{total:.2f} = {rationale}"
