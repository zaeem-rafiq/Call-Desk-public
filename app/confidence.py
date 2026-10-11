"""Transparent confidence score: a weighted sum of three evidence checks.

  prior (0.45)  Snowflake: prior guidance found and the stated change is consistent with it
  web   (0.35)  Nimble: independent web sources corroborate the new value
  price (0.20)  Tiger Data: the market shows a measurable abnormal move vs the benchmark

A failed tool scores 0 for its component, so failures lower confidence instead
of crashing the feed. The rationale string shows every component.

After the verdict, two independent agents re-check the claim (cross_check): Snowflake's Cortex Agent and Nimble's
verification agent. Agreeing adds a little, disagreeing takes more away, and the card shows each step.
"""

from __future__ import annotations

import re

from .models import ConfidenceComponents, CrossCheck, Flag

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


# Agreement adds a little; disagreement takes more away. The total never reaches certainty.
AGREE, DISAGREE, CAP = 0.03, -0.10, 0.98
NIMBLE_WEIGHT = {"high": 1.0, "medium": 0.5}  # a low-trust answer is no signal


def _snowflake_check(flag: Flag) -> CrossCheck | None:
    """The Cortex Agent's verdict against the card's direction (verdict words: see scripts/seed_snowflake.py)."""
    sa = flag.snowflake_agent
    if not sa or sa.status != "done" or sa.mock or sa.verdict not in ("CONSISTENT", "NEW", "CONTRADICTS"):
        return None
    changed = flag.direction in ("cut", "raise")
    if (changed and sa.verdict == "NEW") or (flag.direction == "reaffirm" and sa.verdict == "CONSISTENT") \
            or (flag.direction == "new" and sa.verdict == "NEW"):
        return CrossCheck(source="Snowflake agent", result="agrees", delta=AGREE,
                          reason=f"Snowflake's agent reads the forecast history the same way ({sa.verdict})")
    if changed or flag.direction == "reaffirm":
        return CrossCheck(source="Snowflake agent", result="disagrees", delta=DISAGREE,
                          reason=f"Snowflake's agent reads the forecast history differently ({sa.verdict})")
    return None


# The agent is asked for one word but often writes a phrase ("Revised down from 5%-7% to 2.5%-3.5%").
CHANGE_WORDS = (("reaffirm", r"reaffirm|maintain|unchanged|narrow|reiterat|no change|kept"),
                ("cut", r"\bcut|lower|reduc|down|decreas|trim"),
                ("raise", r"rais|increas|\bup\b|higher|boost"),
                ("new", r"\bnew\b|initiat|introduc|first"))


def _change_word(text: str) -> str:
    t = (text or "").strip().lower()
    return next((word for word, pattern in CHANGE_WORDS if re.search(pattern, t)), "")


def _nimble_check(flag: Flag) -> CrossCheck | None:
    """Nimble's verification agent found the release on its own; does its one-word change match the card?"""
    v = flag.web_desk.verify if flag.web_desk else None
    weight = NIMBLE_WEIGHT.get((v.confidence or "").lower(), 0.0) if v else 0.0
    change = _change_word(str((v.answer or {}).get("change", ""))) if v else ""
    if not weight or change not in ("cut", "raise", "reaffirm", "new"):
        return None
    if change == flag.direction:
        return CrossCheck(source="Nimble check", result="agrees", delta=round(AGREE * weight, 3),
                          reason=f"Nimble's agent found the release and calls it a {change} ({v.confidence} trust)")
    return CrossCheck(source="Nimble check", result="disagrees", delta=round(DISAGREE * weight, 3),
                      reason=f"Nimble's agent found the release and calls it a {change}, not a {flag.direction} ({v.confidence} trust)")


def cross_check(flag: Flag) -> bool:
    """Apply the agents' cross-checks to a checked flag's confidence. Returns True when anything changed.

    Recomputed from the verdict's own score each time, so calling it again as each agent lands is safe.
    Commentary cards (direction n/a) and degraded verdicts are left alone.
    """
    if flag.direction == "n/a" or flag.degraded or flag.confidence is None:
        return False
    base = flag.confidence_base if flag.confidence_base is not None else flag.confidence
    checks = [c for c in (_snowflake_check(flag), _nimble_check(flag)) if c]
    if [c.model_dump() for c in checks] == [c.model_dump() for c in flag.cross_checks]:
        return False
    total = sum(c.delta for c in checks)
    adjusted = max(0.0, min(CAP if total > 0 else 1.0, round(base + total, 2)))
    if total > 0:
        adjusted = max(adjusted, base)  # the cap never lowers a score that was already above it
    head = flag.confidence_rationale.split(" · cross-checks: ")[0]
    note = ", ".join(f"{c.source} {c.result} ({c.delta:+.2f})" for c in checks)
    flag.confidence_base, flag.cross_checks, flag.confidence = (base if checks else None), checks, (adjusted if checks else base)
    flag.confidence_rationale = f"{head} · cross-checks: {note} → {flag.confidence:.2f}" if checks else head
    return True
