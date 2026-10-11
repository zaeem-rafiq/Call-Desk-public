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
    sql: str = ""  # the Tiger Data query that computed the move (shown on the card)
    query_ms: int | None = None  # its server time
    storage: dict = Field(default_factory=dict)  # hypertable columnstore stats, e.g. {"before_bytes", "after_bytes", "ratio", "chunks"}
    peers: list[dict] = Field(default_factory=list)  # [{"ticker", "move", "vs_benchmark", "subject"}] over the same window
    peers_sql: str = ""


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


class GuidancePoint(BaseModel):
    """One earlier statement of the flagged metric's guidance (Snowflake GUIDANCE_LINES)."""

    as_of_date: str
    event: str
    value_text: str
    low: float | None = None
    high: float | None = None
    unit: str = ""
    basis: str = ""  # stated | implied
    status: str = ""  # first given | continues to expect | affirmed
    qualifier: str = ""
    source_url: str = ""


class SnowflakeAgent(BaseModel):
    """Snowflake Cortex Agent's own check of the claim (Cortex Analyst SQL + Cortex Search)."""

    status: Literal["running", "done", "failed", "skipped"] = "running"
    verdict: str = ""  # CONSISTENT | CONTRADICTS | NEW | UNVERIFIABLE
    answer: str = ""
    sql: str = ""
    rows: list[dict] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)  # tools the agent chose, in order
    ms: int | None = None
    error: str | None = None
    mock: bool = False


class WebStep(BaseModel):
    """One Nimble specialist's call on the web desk."""

    id: str  # newswire | primary | serp | sitemap | verify | wall | social | aio | health
    label: str  # e.g. "Newswire scout"
    endpoint: str  # e.g. "POST /v2/search · focus=news"
    phase: Literal["verdict", "enrich"] = "verdict"
    status: Literal["running", "ok", "failed", "skipped"] = "running"
    ms: int | None = None
    summary: str = ""
    feeds: list[str] = Field(default_factory=list)  # step ids whose output this step used
    mock: bool = False


class WebItem(BaseModel):
    title: str = ""
    url: str = ""
    source: str = ""
    snippet: str = ""
    published: str = ""


class TrustClaim(BaseModel):
    claim: str
    confidence: str = ""
    citations: list[WebItem] = Field(default_factory=list)


class WebVerify(BaseModel):
    """Nimble Web Search Agent's independent answer with its trust report."""

    confidence: str = ""  # high | medium | low
    reasoning: str = ""
    answer: dict = Field(default_factory=dict)
    claims: list[TrustClaim] = Field(default_factory=list)
    sources: list[WebItem] = Field(default_factory=list)
    official_sources: int = 0
    run_s: float | None = None
    cached_at: str = ""  # set when this is a stored result of an earlier live agent run (agent runs are billed per task)


class WebDesk(BaseModel):
    steps: list[WebStep] = Field(default_factory=list)
    verify: WebVerify | None = None
    social: list[WebItem] = Field(default_factory=list)
    street: list[WebItem] = Field(default_factory=list)
    wall: dict = Field(default_factory=dict)  # {"domain", "antibots", "driver", "status", "snippet", "url"}
    aio: dict = Field(default_factory=dict)  # {"engine", "text", "stale"}
    health: list[dict] = Field(default_factory=list)  # [{"domain", "status", "success_rate"}]
    mock: bool = False


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
    latency_s: float | None = None  # audio seconds from the quote to the flag appearing
    checked_latency_s: float | None = None  # ... to the fact-check finishing
    sentry_trace_id: str | None = None
    sentry_trace_url: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    mocked: list[str] = Field(default_factory=list)  # integrations that were mocked for this flag
    degraded: bool = False  # the live agent failed; the card shows the claim as heard plus raw tool evidence
    guidance_history: list[GuidancePoint] = Field(default_factory=list)  # Snowflake: every earlier statement of this metric
    snowflake_agent: SnowflakeAgent | None = None  # filled after the verdict
    web_desk: WebDesk | None = None  # Nimble specialists; enrich steps land after the verdict
    alert_text: str = ""  # Gemini TTS: the sentence spoken for this flag
    alert_audio_url: str | None = None  # /static/alerts/<file>.wav, played once by the UI


class CallBrief(BaseModel):
    """End-of-call IR brief written inside Snowflake from the run's flags (CALL_FLAGS + Cortex AI)."""

    status: Literal["running", "done", "failed"] = "running"
    headline: str = ""
    bullets: list[str] = Field(default_factory=list)
    sql: str = ""
    rows_written: int = 0
    ms: int | None = None
    model: str = ""
    error: str | None = None
    mock: bool = False


class TranscriptSegment(BaseModel):
    t: float  # seconds into the clip
    call_ts: str
    speaker: str
    text: str
    chunk: int
