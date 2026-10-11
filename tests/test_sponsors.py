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
