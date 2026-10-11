"""Fast unit tests for parsing, direction, confidence, price math, and the mock pipeline."""

import asyncio
from datetime import datetime

import pytest

from app import config, confidence
from app.claims import numeric_direction, parse_range
from app.hub import EventHub
from app.tools import prices


@pytest.mark.parametrize("value,expected", [
    ("+2.5% to +3.5%", (2.5, 3.5)),
    ("about +6%", (6.0, 6.0)),
    ("low end of +5% to +7%", (5.0, 5.0)),
    ("Approximately 22%", (22.0, 22.0)),
    (None, None),
    ("robust", None),
])
def test_parse_range(value, expected):
    assert parse_range(value) == expected


@pytest.mark.parametrize("prior,new,direction", [
    ("low end of +5% to +7%", "+2.5% to +3.5%", "cut"),
    ("+2% to +4%", "about +3%", "reaffirm"),
    ("+4% to +6%", "about +6%", "raise"),
    ("about 22%", "about 21%", "cut"),
    (None, "+3%", None),
])
def test_numeric_direction(prior, new, direction):
    assert numeric_direction(prior, new) == direction


def test_confidence_failed_tools_lower_score():
    full, _, _ = confidence.combine(confidence.prior_score(True, True, True), confidence.web_score(True, 2, 0, 3), confidence.price_score(True, 1.2))
    failed, comps, rationale = confidence.combine(confidence.prior_score(False, False, None), confidence.web_score(False, 0, 0, 0), confidence.price_score(False, None))
    assert full == 1.0 and failed == 0.0
    assert comps.prior == 0 and "failed" in rationale


@pytest.mark.skipif(not (config.DATA / "prices.csv").exists(), reason="vendor price data is not redistributed; run scripts/fetch_prices.py")
def test_price_reaction_from_csv():
    since = datetime(2026, 10, 8, 8, 20, tzinfo=config.ET).isoformat()
    r = asyncio.run(prices.mock_price_reaction("PEP", since, "XLP"))
    assert r["ticker"] == "PEP" and r["benchmark"] == "XLP"
    assert r["vs_benchmark"] == pytest.approx(r["pct_move"] - r["benchmark_pct_move"], abs=2e-3)  # each side is rounded to 3 dp
    assert r["close_pct_move"] == pytest.approx(3.726, abs=0.01)  # PEP closed +3.73% on Oct 8
    assert len(r["series"]) > 10


def test_mock_pipeline_flags_eps_cut(monkeypatch):
    for name in config.INTEGRATIONS:
        monkeypatch.setenv(f"MOCK_{name.upper()}", "1")
    from app import pipeline

    hub = EventHub()
    hub.start_run("pytest", record=False)
    summary = asyncio.run(pipeline.run_live(hub, speed=0))
    flags = {}
    for item in hub.history:
        if item["event"] == "flag":
            flags[item["data"]["id"]] = item["data"]
    eps = [f for f in flags.values() if f["metric"] == "core_eps_growth"]
    assert summary["state"] == "done"
    assert eps and eps[0]["direction"] == "cut" and eps[0]["status"] == "checked"
    assert eps[0]["new_value"] == "+2.5% to +3.5%" and eps[0]["sentry_trace_id"]
    assert all(f["status"] == "checked" and f["confidence"] is not None for f in flags.values())


def test_hedged_request_returns_the_fast_twin():
    from app import pipeline

    calls = []

    async def make():
        calls.append(1)
        await asyncio.sleep(5 if len(calls) == 1 else 0.05)  # first request hangs, the twin is fast
        return f"result-{len(calls)}"

    start = asyncio.run(asyncio.wait_for(pipeline.hedged(make, hedge_after=0.1), timeout=2))
    assert start == "result-2" and len(calls) == 2


def test_hedged_request_raises_when_all_fail():
    from app import pipeline

    async def make():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.hedged(make, hedge_after=0.1))


def test_late_flag_from_old_run_is_dropped(monkeypatch):
    for name in config.INTEGRATIONS:
        monkeypatch.setenv(f"MOCK_{name.upper()}", "1")
    from app import pipeline

    async def scenario():
        hub = EventHub()
        hub.start_run("old", record=False)
        gate = asyncio.Event()

        async def slow_check(claim, flag, info, publish):
            await gate.wait()  # finishes only after the next run has started
            flag.status = "checked"
            publish(flag)
            return flag

        monkeypatch.setattr(pipeline, "fact_check", slow_check)
        run = asyncio.create_task(pipeline.run_live(hub, speed=0))
        while not any(e["event"] == "flag" for e in hub.history):
            await asyncio.sleep(0.05)
        hub.start_run("new", record=False)
        gate.set()
        await run
        return hub.history

    history = asyncio.run(scenario())
    assert not [e for e in history if e["event"] == "flag"]


def test_one_card_per_metric_but_guidance_keeps_its_own():
    from app.claims import Claim, card_slot

    def c(metric, kind, value=None):
        return Claim(quote="q", speaker="s", claim_type=kind, metric=metric, direction="n/a", new_value=value, materiality=0.8)

    assert card_slot(c("outlook_next_year", "guidance", None)) == card_slot(c("outlook_next_year", "guidance", "February"))
    assert card_slot(c("organic_revenue_growth", "guidance", "robust")) == card_slot(c("organic_revenue_growth", "guidance", "long-term 4%"))
    assert card_slot(c("core_eps_growth", "kpi", "2%")) != card_slot(c("core_eps_growth", "guidance", None))
    assert card_slot(c("other", "tone", "a")) != card_slot(c("other", "tone", "b"))
