"""Exercise the live Gemini code paths against a fake client (no network, no key)."""

import asyncio
from types import SimpleNamespace

from app import config, gemini
from app.claims import ChunkResult, Claim, Segment
from app.factcheck import fact_check
from app import tools
from app.models import Flag, PriceReaction


class FakeModels:
    def __init__(self):
        self.calls = []

    async def generate_content(self, model, contents, config):
        self.calls.append(config)
        if config.tools:  # agent step: ask for two tools; the orchestrator must add the third
            fc = [SimpleNamespace(id="c1", name="search_prior_guidance", args={"query": "PepsiCo core EPS guidance Q2 2026"}),
                  SimpleNamespace(id="c2", name="search_live_web", args={"query": "PepsiCo Q3 2026 core EPS guidance"})]
            return SimpleNamespace(function_calls=fc, parsed=None, text=None)
        if config.response_schema is ChunkResult:
            parsed = ChunkResult(segments=[Segment(start=1.0, speaker="Steve Schmitt (CFO)", text="It's the margin piece that's driving the guide down in EPS.")],
                                 claims=[Claim(quote="driving the guide down in EPS", speaker="Steve Schmitt (CFO)", claim_type="guidance",
                                               metric="core_eps_growth", direction="cut", materiality=0.95, start=1.0)])
            return SimpleNamespace(function_calls=None, parsed=parsed, text=parsed.model_dump_json())
        parsed = gemini.Synthesis(
            direction="cut", new_value="+2.5% to +3.5%", new_value_source="Q3 2026 release", prior_value="low end of +5% to +7%",
            prior_found=True, consistent_with_prior=True, what_changed="Core EPS growth cut to +2.5% to +3.5%.",
            why_it_matters="Margins, not demand.", evidence=[gemini.EvidenceRef(source="prior", index=1, stance="supports"),
                                                             gemini.EvidenceRef(source="web", index=1, stance="supports")],
            related_changes=[gemini.SynthRelated(metric_label="Core tax rate", prior_value="about 22%", new_value="about 21%", direction="cut")])
        return SimpleNamespace(function_calls=None, parsed=parsed, text=parsed.model_dump_json())


def _fake_client(monkeypatch):
    models = FakeModels()
    monkeypatch.setattr(gemini, "client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))
    return models


def test_listener_parses_chunk(monkeypatch):
    _fake_client(monkeypatch)
    out = asyncio.run(gemini.transcribe_and_extract(b"RIFF", [], 12, 144.0, 156.0, config.call_info()))
    assert out.claims[0].metric == "core_eps_growth" and out.segments[0].speaker.startswith("Steve")


def test_agent_runs_all_three_tools_and_applies_verdict(monkeypatch):
    for name in ("nimble", "tiger", "snowflake", "sentry"):
        monkeypatch.setenv(f"MOCK_{name.upper()}", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("MOCK_GEMINI", "0")
    models = _fake_client(monkeypatch)

    async def fixed_price(ticker, since_ts, benchmark="XLP"):  # independent of data/prices.csv (not in the public snapshot)
        return PriceReaction(ticker=ticker, benchmark=benchmark, pct_move=-0.83, benchmark_pct_move=0.07, vs_benchmark=-0.9,
                             window="08:20 → 08:50 ET", since_ts=since_ts, source="test fixture", mock=True).model_dump()
    monkeypatch.setattr(tools.prices, "mock_price_reaction", fixed_price)
    claim = Claim(quote="driving the guide down in EPS", speaker="Steve Schmitt (CFO)", claim_type="guidance",
                  metric="core_eps_growth", direction="cut", materiality=0.95)
    flag = Flag(id="f1", call_ts="2026-10-08T08:20:17-04:00", t_offset=147.2, speaker=claim.speaker, quote=claim.quote,
                claim_type="guidance", metric="core_eps_growth", metric_label="Core EPS growth", direction="cut")
    published = []
    out = asyncio.run(fact_check(claim, flag, config.call_info(), lambda f: published.append(f.model_copy(deep=True))))
    assert {t.tool for t in out.tool_calls} == {"search_prior_guidance", "search_live_web", "get_price_reaction"}
    assert out.status == "checked" and out.new_value == "+2.5% to +3.5%" and out.new_value_source == "Q3 2026 release"
    assert out.direction == "cut" and out.related_changes[0].metric_label == "Core tax rate"
    assert any(e.source.startswith("Snowflake") for e in out.evidence) and any(e.source.startswith("Nimble") for e in out.evidence)
    assert out.confidence and out.confidence > 0.7
    assert published[1].price_reaction is not None and published[1].status == "checking"  # chart shows before the verdict
    assert len(models.calls) == 2  # one planning call, one verdict call
