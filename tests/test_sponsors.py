"""Snowflake and Nimble integration logic that runs without keys."""

import asyncio
from pathlib import Path

from app import config
from app.tools import live_web, prior_guidance, snowflake_agent


def test_transcripts_never_reach_snowflake(tmp_path, monkeypatch):
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "q2_2026_transcript.txt").write_text("Steve Schmitt: we may be towards the low end.\n\nAnalyst: thanks.")
    (prior / "q2_2026_guidance.md").write_text("# Guidance\n\n## Guidance table\n\n| Metric | Value | Basis |\n|---|---|---|\n| Core EPS | +5% to +7% | Implied |\n")
    chunks = prior_guidance.load_prior_chunks(sources=[(prior, "PepsiCo", "PEP")])
    assert chunks, "the guidance file must still load"
    assert not [c for c in chunks if "transcript" in c["DOC_ID"].lower() or c["DOC_TYPE"] == "transcript"]


def test_prior_tool_returns_search_hits_then_history():
    out = asyncio.run(prior_guidance.mock_search_prior_guidance("PepsiCo fiscal 2026 core EPS guidance", metric="core_eps_growth"))
    assert [h["as_of_date"] for h in out["history"]] == ["2025-12-08", "2026-02-03", "2026-04-16", "2026-07-09"]
    assert out["history"][-1]["qualifier"].startswith("CFO on the Q2 call")
    assert out["hits"][0]["doc_type"] != "guidance_history", "the top guidance row stays [P1]"
    assert out["hits"][-1]["doc_type"] == "guidance_history" and "2025-12-08" in out["hits"][-1]["snippet"]


def test_guidance_table_rows_from_release_markdown():
    page = """
| | 2026 Guidance | |
|---|---|---|
| | Current | Previous |
| Organic Revenue | Approximately +3% | +2% to +4% |
| Core EPS | +2.5% to +3.5% | Low-end of +5% to +7% |
| Total | 19 | (16) |
| Core Annual Effective Tax Rate ^(c) | Approximately 21% | Approximately 22% |
"""
    table = live_web.guidance_table(page)
    assert "Core EPS | +2.5% to +3.5% | Low-end of +5% to +7%" in table
    assert "Organic Revenue | Approximately +3% | +2% to +4%" in table
    assert "Total" not in table
    hit = live_web.ir_hit({"url": "https://investors.pepsico.com/q3-2026-prepared-management-remarks.pdf", "title": "PMR", "description": page})
    assert hit and hit["primary"] and hit["source"] == "investors.pepsico.com"


AGENT_RESPONSE = {
    "role": "assistant",
    "content": [
        {"type": "text", "text": "I'll check the guidance history for PepsiCo's organic revenue growth metric."},
        {"type": "tool_use", "tool_use": {"name": "Guidance_History", "type": "system_agentic_semantic_context", "input": {}}},
        {"type": "tool_use", "tool_use": {"name": "Prior_Docs_Search", "type": "cortex_search", "input": {"query": "organic revenue"}}},
        {"type": "tool_use", "tool_use": {"name": "system_execute_sql", "type": "system_execute_sql", "input": {"sql": "SELECT 1"}}},
        {"type": "tool_result", "tool_result": {"name": "Prior_Docs_Search", "type": "cortex_search", "status": "success", "content": [
            {"type": "json", "json": {"search_results": [{"doc_id": "q2_2026_guidance.md#12", "doc_title": "PepsiCo Q2 2026 earnings release outlook"}]}}]}},
        {"type": "tool_result", "tool_result": {"name": "system_execute_sql", "type": "system_execute_sql", "status": "success", "content": [
            {"type": "json", "json": {"sql": "SELECT as_of_date, value_text FROM __guidance WHERE metric = 'organic_revenue_growth' ORDER BY as_of_date",
                                      "result_set": {"resultSetMetaData": {"rowType": [{"name": "AS_OF_DATE"}, {"name": "VALUE_TEXT"}]},
                                                     "data": [["2025-12-08", "+2% to +4%"], ["2026-07-09", "+2% to +4%"]]}}}]}},
        {"type": "thinking", "thinking": {"text": "..."}},
        {"type": "text", "text": "CONSISTENT. Before the Q3 2026 call, PepsiCo guided organic revenue growth to +2% to +4%.", "annotations": []},
        {"type": "suggested_queries", "suggested_queries": []},
    ],
}


def test_snowflake_agent_response_parse():
    out = snowflake_agent.parse(AGENT_RESPONSE)
    assert out.verdict == "CONSISTENT" and out.answer.startswith("CONSISTENT.")
    assert out.tools == ["Cortex Analyst · semantic view", "Cortex Search", "SQL on warehouse"]
    assert out.sql.startswith("SELECT as_of_date") and out.rows[-1] == {"AS_OF_DATE": "2026-07-09", "VALUE_TEXT": "+2% to +4%"}
    assert out.citations == ["PepsiCo Q2 2026 earnings release outlook"]


def test_sentry_starts_when_its_key_appears_after_server_start(monkeypatch):
    from app import observability

    calls = []
    monkeypatch.setattr(observability, "_enabled", False)
    monkeypatch.setattr(observability, "init_sentry", lambda: calls.append(1) or setattr(observability, "_enabled", True) or True)
    monkeypatch.setenv("MOCK_SENTRY", "1")
    assert observability.trace_url("abc") is None and not calls  # still mocked: never started
    monkeypatch.setenv("MOCK_SENTRY", "0")
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    assert observability.trace_url("abc") == "https://acme.sentry.io/explore/traces/trace/abc/"
    assert observability.trace_url("def") and calls == [1]  # started once


def _slow_nimble(monkeypatch, seconds=5.0):
    from app.tools import nimble

    async def slow(*a, **k):
        await asyncio.sleep(seconds)
        return {}
    for name in ("agent_start", "agent_wait", "domain_knowledge", "template_run", "search", "extract"):
        monkeypatch.setattr(nimble, name, slow)


def test_stopping_a_run_silences_the_web_desk(monkeypatch):
    """A cancelled fact-check must not leave Nimble specialists publishing into the next run."""
    from app.models import WebDesk
    from app.tools import web_desk

    _slow_nimble(monkeypatch)
    calls = []

    async def main():
        desk = WebDesk()
        task = asyncio.create_task(web_desk.enrich(desk, company="PepsiCo", ticker="PEP", metric_label="Core EPS growth", new_value=None,
                                                   found={"broad": [{"url": "https://www.cnbc.com/x", "title": "x"}]}, mock=False,
                                                   on_change=lambda: calls.append(asyncio.get_running_loop().time())))
        await asyncio.sleep(0.2)
        task.cancel()
        stopped = asyncio.get_running_loop().time()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.5)
        return stopped, desk

    stopped, desk = asyncio.run(main())
    assert not [t for t in calls if t > stopped + 0.05], "a specialist published after the run stopped"
    assert all(s.status != "running" for s in desk.steps)


def test_a_skipped_step_is_not_shown_as_ok(monkeypatch):
    from app.models import WebDesk
    from app.tools import web_desk

    _slow_nimble(monkeypatch, seconds=0.01)
    desk = WebDesk()
    asyncio.run(web_desk.enrich(desk, company="PepsiCo", ticker="PEP", metric_label="Core EPS growth", new_value=None,
                                found={"broad": []}, mock=False, on_change=lambda: None))
    wall = next(s for s in desk.steps if s.id == "wall")
    assert wall.status == "skipped" and wall.summary == "no news article to open"


def test_nimble_billing_limit_shows_as_unavailable(monkeypatch):
    from app.models import WebDesk
    from app.tools import nimble, web_desk

    _slow_nimble(monkeypatch, seconds=0.01)

    async def billing(*a, **k):
        raise nimble.NimbleError("POST /v2/agents/runs -> 402: {'detail': {'message': 'This account cannot be billed'}}")
    monkeypatch.setattr(nimble, "agent_start", billing)
    monkeypatch.setattr(web_desk, "_agents_unbillable_until", 0.0)
    monkeypatch.setattr(web_desk, "VERIFY_CACHE", Path("/nonexistent/verify.json"))
    desk = WebDesk()
    asyncio.run(web_desk.enrich(desk, company="PepsiCo", ticker="PEP", metric_label="Core EPS growth", new_value="+2.5% to +3.5%",
                                found={"broad": []}, mock=False, on_change=lambda: None))
    verify = next(s for s in desk.steps if s.id == "verify")
    assert verify.status == "skipped" and verify.summary.startswith("unavailable")


def test_verification_agent_runs_only_on_numeric_lines_and_reuses_stored_results(monkeypatch, tmp_path):
    """Agent runs are billed per task: non-numeric flags never run one, and a stored live result is reused, labelled."""
    import json

    from app.models import WebDesk, WebVerify
    from app.tools import nimble, web_desk

    _slow_nimble(monkeypatch, seconds=0.01)
    runs = []

    async def start(*a, **k):
        runs.append(1)
        return "wsa_1", f"run_{len(runs)}"

    async def wait(*a, **k):
        return {"output": {"type": "json", "content": {"guidance_in_oct_8_2026_release": "+2.5% to +3.5%"},
                           "trust": {"confidence": "high", "sources": [{"type": "primary", "url": "https://investors.pepsico.com/x"}]}},
                "run_s": 12.0}
    monkeypatch.setattr(nimble, "agent_start", start)
    monkeypatch.setattr(nimble, "agent_wait", wait)
    monkeypatch.setattr(web_desk, "_agents_unbillable_until", 0.0)
    monkeypatch.setattr(web_desk, "VERIFY_CACHE", tmp_path / "verify.json")

    def run(label, value):
        desk = WebDesk()
        asyncio.run(web_desk.enrich(desk, company="PepsiCo", ticker="PEP", metric_label=label, new_value=value,
                                    found={"broad": []}, mock=False, on_change=lambda: None))
        return desk, next(s for s in desk.steps if s.id == "verify")

    _, step = run("Next-year outlook", "in Feb when we report the fourth quarter")
    assert step.status == "skipped" and "not a numeric" in step.summary and not runs
    desk, step = run("Core EPS growth", "+2.5% to +3.5%")
    assert step.status == "ok" and len(runs) == 1 and desk.verify.answer["now"] == "+2.5% to +3.5%"
    assert "pepsico|core eps growth" in json.loads((tmp_path / "verify.json").read_text())
    desk, step = run("Core EPS growth", "+2.5% to +3.5%")
    assert len(runs) == 1, "a stored result must be reused, not re-billed"
    assert desk.verify.cached_at and "cached result" in step.summary


def test_snowflake_agent_runs_do_not_queue_behind_each_other(monkeypatch):
    """Each agent run takes its own connection, so three close flags finish together instead of timing out in turn."""
    import json
    import time

    from app.tools import prior_guidance, snowflake_agent

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): time.sleep(0.4)
        def fetchone(self): return [json.dumps({"content": [{"type": "text", "text": "CONSISTENT. ok"}]})]

    class Conn:
        def cursor(self): return Cur()
        def is_closed(self): return False
        def close(self): pass

    monkeypatch.setattr(prior_guidance, "connect", lambda *a, **k: Conn())
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "CALLDESK")
    monkeypatch.setenv("SNOWFLAKE_SCHEMA", "PRIOR")

    async def main():
        started = time.monotonic()
        outs = await asyncio.gather(*(snowflake_agent.live_check("PepsiCo", "PEP", "q", "Core EPS growth", None) for _ in range(3)))
        return time.monotonic() - started, outs

    took, outs = asyncio.run(main())
    assert all(o.verdict == "CONSISTENT" for o in outs)
    assert took < 1.0, f"agent runs were serialized ({took:.2f}s for three 0.4 s runs)"


def test_verdict_labels_need_numbers_and_never_show_internal_ids():
    from app.claims import Claim
    from app.factcheck import _apply_synthesis
    from app.models import Flag

    claim = Claim(quote="margins below plan", speaker="CFO", claim_type="margin", metric="operating_margin", direction="cut", materiality=0.8)
    flag = Flag(id="f", call_ts="2026-10-08T08:20:17-04:00", t_offset=1.0, speaker="CFO", quote=claim.quote, claim_type="margin",
                metric="operating_margin", metric_label="Operating margin", direction="cut")
    web = [{"source": "investors.pepsico.com", "url": "https://investors.pepsico.com/x"}] * 4
    _apply_synthesis(flag, claim, {"direction": "cut", "new_value": "below plan", "new_value_source": "W4"}, [], web, None, None)
    assert flag.direction == "n/a", "no CUT badge without numbers on both sides"
    assert flag.new_value_source == "investors.pepsico.com"
    _apply_synthesis(flag, claim, {"direction": "cut", "new_value": "+2.5% to +3.5%", "prior_value": "low end of +5% to +7%",
                                   "new_value_source": "Q3 2026 release [W1]"}, [], web, None, None)
    assert flag.direction == "cut" and flag.new_value_source == "Q3 2026 release investors.pepsico.com"


def test_tagged_cortex_verdict_line_wins_over_prose():
    from app.tools import snowflake_agent

    out = snowflake_agent.parse({"content": [{"type": "text", "text": "VERDICT: CONSISTENT\nIt does not CONTRADICT; the range +2% to +4% holds."}]})
    assert out.verdict == "CONSISTENT" and out.answer.startswith("It does not")


def test_a_billed_run_past_its_deadline_is_collected_not_repeated(monkeypatch, tmp_path):
    """A run still working at the flag's deadline keeps its ids; the next flag collects it instead of paying again.
    Low-confidence results are stored too."""
    import json

    from app.models import WebDesk
    from app.tools import nimble, web_desk

    _slow_nimble(monkeypatch, seconds=0.01)
    started, polls = [], []

    async def start(*a, **k):
        started.append(1)
        return "wsa_1", "run_1"

    async def wait(http, agent_id, run_id, **k):
        polls.append(run_id)
        if len(polls) == 1:
            raise nimble.AgentPending(agent_id, run_id, "running")
        return {"output": {"type": "json", "content": {"guidance_in_oct_8_2026_release": "+2.5% to +3.5%"},
                           "trust": {"confidence": "low", "sources": []}}, "run_s": 3.0}

    async def no_background(*a, **k):  # keep the test deterministic: the next flag does the collecting
        return None
    monkeypatch.setattr(nimble, "agent_start", start)
    monkeypatch.setattr(nimble, "agent_wait", wait)
    monkeypatch.setattr(web_desk, "_finish_pending", no_background)
    monkeypatch.setattr(web_desk, "_agents_unbillable_until", 0.0)
    monkeypatch.setattr(web_desk, "VERIFY_CACHE", tmp_path / "verify.json")

    def run():
        desk = WebDesk()
        asyncio.run(web_desk.enrich(desk, company="PepsiCo", ticker="PEP", metric_label="Core EPS growth", new_value="+2.5% to +3.5%",
                                    found={"broad": []}, mock=False, on_change=lambda: None))
        return desk, next(s for s in desk.steps if s.id == "verify")

    _, step = run()
    assert step.status == "skipped" and "still working" in step.summary
    assert json.loads((tmp_path / "verify.json").read_text())["pepsico|core eps growth"]["pending"]["run_id"] == "run_1"
    desk, step = run()
    assert started == [1], "the pending run was collected, not paid for again"
    assert polls == ["run_1", "run_1"] and desk.verify.confidence == "low"
    stored = json.loads((tmp_path / "verify.json").read_text())["pepsico|core eps growth"]
    assert stored["confidence"] == "low" and "pending" not in stored
    run()
    assert started == [1] and len(polls) == 2, "a stored low-confidence result is reused too"


def test_concurrent_flags_share_one_fresh_agent_run(monkeypatch, tmp_path):
    """On stage (NIMBLE_VERIFY_FRESH=1) two flags for the same metric at once must pay for one run, not two."""
    from app.models import WebDesk
    from app.tools import nimble, web_desk

    _slow_nimble(monkeypatch, seconds=0.01)
    started = []

    async def start(*a, **k):
        started.append(1)
        await asyncio.sleep(0.05)
        return "wsa_1", "run_1"

    async def wait(*a, **k):
        await asyncio.sleep(0.2)
        return {"output": {"type": "json", "content": {"guidance_in_oct_8_2026_release": "+2.5% to +3.5%"},
                           "trust": {"confidence": "high", "sources": []}}, "run_s": 0.2}
    monkeypatch.setattr(nimble, "agent_start", start)
    monkeypatch.setattr(nimble, "agent_wait", wait)
    monkeypatch.setattr(web_desk, "_agents_unbillable_until", 0.0)
    monkeypatch.setattr(web_desk, "VERIFY_CACHE", tmp_path / "verify.json")
    monkeypatch.setenv("NIMBLE_VERIFY_FRESH", "1")

    async def two():
        desks = [WebDesk(), WebDesk()]
        await asyncio.gather(*(web_desk.enrich(d, company="PepsiCo", ticker="PEP", metric_label="Core EPS growth", new_value="+2.5% to +3.5%",
                                               found={"broad": []}, mock=False, on_change=lambda: None) for d in desks))
        return desks

    desks = asyncio.run(two())
    assert started == [1], f"{len(started)} billed runs for one metric"
    assert all(d.verify and d.verify.confidence == "high" for d in desks)


def test_listener_slugs_and_periods_map_to_each_companys_guidance_lines():
    from app.tools import guidance_history as g

    g._csv_rows.cache_clear()
    cases = {("DAL", "eps", "FY2026"): "eps", ("DAL", "eps", "Q4 2026"): "q4_eps", ("DAL", "operating_margin", "Q3 2026"): "q3_operating_margin",
             ("DAL", "leverage", None): "gross_leverage", ("DAL", "unit_cost_growth", None): "nonfuel_unit_cost_growth",
             ("DAL", "capacity", "Q4 2026"): "q4_capacity_growth", ("DAL", "fuel_price", "Q3 2026"): "q3_fuel_price",
             ("PEP", "core_eps_growth", "FY2026"): "core_eps_growth"}
    if "DAL" not in set(g._csv_rows()["TICKER"]):
        cases = {k: v for k, v in cases.items() if k[0] == "PEP"}
    for (ticker, metric, period), want in cases.items():
        assert g.resolve_metric(ticker, metric, period) == want, (ticker, metric, period)
    assert g.resolve_metric("PEP", "operating_margin", "Q4 2026") == "operating_margin"  # no line: unchanged, history empty


def test_only_unit_bearing_guidance_numbers_can_start_a_billed_agent_run():
    from app.tools.web_desk import verifiable

    assert not verifiable("outlook_next_year", "detailed expectations to 2027"), "a year is not a guidance number"
    assert not verifiable("eps", "detailed expectations to 2027")
    assert not verifiable("other", "+3%") and not verifiable("macro", "+3%")
    assert not verifiable("organic_revenue_growth", "robust") and not verifiable("eps", "Q4 2026")
    for metric, value in [("core_eps_growth", "+2.5% to +3.5%"), ("eps", "$5.10 to $5.60"), ("leverage", "2.2x"),
                          ("free_cash_flow", "approximately $2.5 billion"), ("organic_revenue_growth", "approximately +3%")]:
        assert verifiable(metric, value), (metric, value)
