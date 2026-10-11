"""Snowflake and Nimble integration logic that runs without keys."""

import asyncio

from app import config
from app.tools import live_web, prior_guidance, snowflake_agent


def test_transcripts_never_reach_snowflake(tmp_path, monkeypatch):
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "q2_2026_transcript.txt").write_text("Steve Schmitt: we may be towards the low end.\n\nAnalyst: thanks.")
    (prior / "q2_2026_guidance.md").write_text("# Guidance\n\n## Guidance table\n\n| Metric | Value | Basis |\n|---|---|---|\n| Core EPS | +5% to +7% | Implied |\n")
    monkeypatch.setattr(config, "DATA", tmp_path)
    chunks = prior_guidance.load_prior_chunks()
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
    for name in ("agent_run", "domain_knowledge", "template_run", "search", "extract"):
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
    monkeypatch.setattr(nimble, "agent_run", billing)
    monkeypatch.setattr(web_desk, "_agents_unbillable_until", 0.0)
    desk = WebDesk()
    asyncio.run(web_desk.enrich(desk, company="PepsiCo", ticker="PEP", metric_label="Core EPS growth", new_value=None,
                                found={"broad": []}, mock=False, on_change=lambda: None))
    verify = next(s for s in desk.steps if s.id == "verify")
    assert verify.status == "skipped" and verify.summary.startswith("unavailable")


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
