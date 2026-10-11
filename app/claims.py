"""Claim types produced by transcription/extraction, plus dedupe and direction checks."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .models import Direction

# Canonical metrics. Gemini must pick one slug so repeated mentions dedupe.
METRICS: dict[str, str] = {
    "core_eps_growth": "Core EPS growth",
    "core_cc_eps_growth": "Core constant-currency EPS growth",
    "organic_revenue_growth": "Organic revenue growth",
    "net_revenue_growth": "Net revenue growth",
    "core_tax_rate": "Core tax rate",
    "gross_margin": "Gross margin",
    "operating_margin": "Operating margin",
    "north_america_margin": "North America margin",
    "tariffs": "Tariffs",
    "pricing": "Pricing",
    "volume": "Volume",
    "fx": "FX impact",
    "capex": "Capex",
    "buyback": "Buybacks",
    "dividend": "Dividend",
    "cash_returns": "Cash returns",
    "productivity": "Productivity savings",
    "outlook_next_year": "Next-year outlook",
    "macro": "Macro / consumer",
    "other": "Other",
}


class Segment(BaseModel):
    start: float = Field(description="Seconds from the start of this audio chunk where the turn begins")
    speaker: str = Field(description="Speaker name and role if known, e.g. 'Jamie Caulfield (CFO)'")
    text: str = Field(description="Verbatim transcript of what was said")


class Claim(BaseModel):
    quote: str = Field(description="Shortest verbatim quote that states the claim")
    speaker: str
    claim_type: str = Field(description="guidance | kpi | margin | capex | buyback | pricing | tariffs | macro | tone")
    metric: str = Field(description="One slug from the metric list")
    direction: Direction = Field(description="cut | raise | reaffirm | new | n/a, versus prior guidance")
    prior_value: str | None = Field(default=None, description="Prior value if the speaker states it, else null")
    new_value: str | None = Field(default=None, description="New or current value as stated, e.g. '+2.5% to +3.5%'")
    materiality: float = Field(description="0-1: how much this could move the stock or peers' models")
    start: float = Field(default=0.0, description="Seconds from the start of this chunk where the quote begins")


class ChunkResult(BaseModel):
    segments: list[Segment] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)


def metric_label(slug: str) -> str:
    return METRICS.get(slug, slug.replace("_", " ").capitalize())


_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_range(value: str | None) -> tuple[float, float] | None:
    """'+2.5% to +3.5%' -> (2.5, 3.5); 'about 6%' -> (6, 6); 'low end of 5 to 7%' -> (5, 5)."""
    if not value:
        return None
    nums = [float(n) for n in _NUM.findall(value.replace(",", ""))]
    if not nums:
        return None
    if "low end" in value.lower():
        return (nums[0], nums[0])
    if "high end" in value.lower():
        return (nums[-1], nums[-1])
    return (min(nums[:2]), max(nums[:2])) if len(nums) >= 2 else (nums[0], nums[0])


def numeric_direction(prior: str | None, new: str | None) -> Direction | None:
    """Direction implied by the numbers alone, or None when not comparable."""
    p, n = parse_range(prior), parse_range(new)
    if not (p and n):
        return None
    pm, nm = sum(p) / 2, sum(n) / 2
    if abs(nm - pm) < 0.05:
        return "reaffirm"
    return "raise" if nm > pm else "cut"


def dedupe_key(claim: Claim) -> str:
    rng = parse_range(claim.new_value)
    value = f"{rng[0]:g}-{rng[1]:g}" if rng else (claim.new_value or "").lower().strip()
    return f"{claim.metric}|{value}"
