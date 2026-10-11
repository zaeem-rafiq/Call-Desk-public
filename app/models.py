"""Flag schema shared by the pipeline, the API, the replay cache, and the eval."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Direction = Literal["cut", "raise", "reaffirm", "new", "n/a"]
Stance = Literal["supports", "contradicts", "neutral"]


class Evidence(BaseModel):
    source: str  # e.g. "Snowflake Cortex Search", "Nimble · reuters.com", "Tiger Data"
    ref: str  # URL, document id, or query
    snippet: str
    stance: Stance = "neutral"
    mock: bool = False


class PricePoint(BaseModel):
    ts: str  # ISO 8601
    pct: float  # % change vs reference price
    bench_pct: float


class PriceReaction(BaseModel):
    ticker: str
    benchmark: str
    pct_move: float | None = None  # ticker % move over the window
    benchmark_pct_move: float | None = None
    vs_benchmark: float | None = None  # abnormal move in percentage points
    window: str = ""  # human label, e.g. "08:31 → 09:01 ET"
    since_ts: str | None = None
    until_ts: str | None = None
    day_pct_move: float | None = None  # vs prior close, as of until_ts
    day_vs_benchmark: float | None = None
    close_pct_move: float | None = None  # official close vs prior close
    close_benchmark_pct_move: float | None = None
    close_vs_benchmark: float | None = None
    series: list[PricePoint] = Field(default_factory=list)
    source: str = ""
    mock: bool = False


class ConfidenceComponents(BaseModel):
    prior: float = 0.0  # consistency with prior guidance (Snowflake)
    web: float = 0.0  # web corroboration (Nimble)
    price: float = 0.0  # price confirmation (Tiger Data)


class ToolCall(BaseModel):
    tool: str
    backend: str  # snowflake | nimble | tiger
    args: dict
    ok: bool
    ms: int
    mock: bool = False
    error: str | None = None


class RelatedChange(BaseModel):
    """One row of the guidance table the agent found (e.g. in the morning release)."""

    metric_label: str
    prior_value: str | None = None
    new_value: str
    direction: Direction


class Flag(BaseModel):
    id: str
    status: Literal["checking", "checked"] = "checking"
    call_ts: str  # ISO 8601, real call time (ET)
    t_offset: float  # seconds into the clip
    speaker: str
    quote: str
    claim_type: str  # guidance | kpi | margin | capex | buyback | pricing | tariffs | macro | tone
    metric: str  # canonical slug, e.g. core_eps_growth
    metric_label: str
    direction: Direction
    prior_value: str | None = None
    new_value: str | None = None
    new_value_source: str = "spoken"  # "spoken", or where the agent found the number
    what_changed: str = ""
    why_it_matters: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    related_changes: list[RelatedChange] = Field(default_factory=list)
    price_reaction: PriceReaction | None = None
    confidence: float | None = None
    confidence_components: ConfidenceComponents | None = None
    confidence_rationale: str = ""
    materiality: float = 0.0
    sentry_trace_id: str | None = None
    sentry_trace_url: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    mocked: list[str] = Field(default_factory=list)  # integrations that were mocked for this flag


class TranscriptSegment(BaseModel):
    t: float  # seconds into the clip
    call_ts: str
    speaker: str
    text: str
    chunk: int
