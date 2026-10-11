"""End-of-call Snowflake brief: mock path, live path against a fake connection, and the no-transcript rule."""

import asyncio
import json
import re
import time

import pytest

from app import briefing, config
from app.models import Flag
from app.tools import prior_guidance

INFO = config.call_info()


def _flags() -> list[Flag]:
    flags = briefing.self_check_flags(INFO)
    for f in flags:
        f.quote, f.speaker = "we now expect core EPS growth of two and a half to three and a half", "Jamie Caulfield"
    return flags


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = -1
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None, timeout=None):
        self.conn.calls.append(("execute", sql, params))
        if self.conn.sleep:
            time.sleep(self.conn.sleep)
        if "AI_COMPLETE" in sql:
            outcome = self.conn.models.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            self._result = (outcome, 2)

    def executemany(self, sql, seq, timeout=None):
        self.conn.calls.append(("executemany", sql, list(seq)))
        self.rowcount = len(self.conn.calls[-1][2])

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, models, sleep=0.0):
        self.calls, self.models, self.sleep = [], list(models), sleep

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self)


@pytest.fixture
def live(monkeypatch):
    """Snowflake 'live' with a fake connection; returns a setter for the AI_COMPLETE outcomes."""
    holder = {}
    monkeypatch.setattr(config, "is_mock", lambda name: False)

    def use(models, sleep=0.0):
        holder["conn"] = FakeConn(models, sleep)
        monkeypatch.setattr(prior_guidance, "connect", lambda *a, **k: holder["conn"])
        return holder["conn"]

    return use


def test_mock_brief_is_instant_and_deterministic(monkeypatch):
    monkeypatch.setenv("MOCK_SNOWFLAKE", "1")
    started = time.monotonic()
    b = asyncio.run(briefing.call_brief(_flags(), INFO))
    assert time.monotonic() - started < 0.2
    assert b.status == "done" and b.mock and b.rows_written == 2
    assert b.headline == "PepsiCo cut Core EPS growth to +2.5% to +3.5% (was low end of +5% to +7%)"
    assert 3 <= len(b.bullets) <= 5
    assert "AI_COMPLETE" in b.sql and "CALL_FLAGS" in b.sql
    assert b == asyncio.run(briefing.call_brief(_flags(), INFO, run_id=re.search(r"RUN_ID = '([^']+)'", b.sql).group(1)))


def test_no_flags_is_a_short_failure(monkeypatch):
    monkeypatch.setenv("MOCK_SNOWFLAKE", "1")
    b = asyncio.run(briefing.call_brief([], INFO))
    assert b.status == "failed" and b.error == "no checked flags to brief"


def test_rows_hold_no_transcript_text():
    for f in _flags():
        row = briefing.flag_row("run-1", f, "PEP", "2026-10-11T16:00:00+00:00")
        assert len(row) == len(briefing.COLUMNS)
        assert f.quote not in row and f.speaker not in row
    assert "QUOTE" not in briefing.ddl() and "SPEAKER" not in briefing.ddl()


def test_parse_brief_shapes():
    good = {"headline": " PepsiCo cut  EPS ", "bullets": ["- one", "two", "", 3]}
    assert briefing.parse_brief(good) == ("PepsiCo cut EPS", ["one", "two"])
    assert briefing.parse_brief(json.dumps(good))[0] == "PepsiCo cut EPS"
    assert briefing.parse_brief(json.dumps(json.dumps(good)))[0] == "PepsiCo cut EPS"
    assert briefing.parse_brief("```json\n" + json.dumps(good) + "\n```")[1] == ["one", "two"]
    with pytest.raises(ValueError):
        briefing.parse_brief({"headline": "x", "bullets": []})


def test_sql_has_only_bound_placeholders():
    leftover = re.sub(r"%\((run_id|model|instructions)\)s", "", briefing.BRIEF_SQL)
    assert "%" not in leftover, "a bare % would break the connector's pyformat binding"
    shown = briefing.display_sql("CALLDESK.PRIOR.CALL_FLAGS", "PEP-x", "claude-sonnet-4-6", "it's plain")
    assert "%(" not in shown and "'PEP-x'" in shown and "'it''s plain'" in shown


def test_live_writes_rows_then_briefs_in_snowflake(live):
    conn = live([json.dumps({"headline": "PepsiCo cut core EPS growth", "bullets": ["a", "b", "c"]})])
    b = asyncio.run(briefing.call_brief(_flags(), INFO, run_id="run-1"))
    assert b.status == "done" and not b.mock and b.model == "claude-sonnet-4-6" and b.rows_written == 2
    assert b.bullets == ["a", "b", "c"] and "RUN_ID = 'run-1'" in b.sql
    kinds = [c[0] for c in conn.calls]
    assert kinds == ["execute", "executemany", "execute"]
    assert "CREATE TABLE IF NOT EXISTS" in conn.calls[0][1]
    assert [r[0] for r in conn.calls[1][2]] == ["run-1", "run-1"]
    assert conn.calls[2][2]["run_id"] == "run-1" and conn.calls[2][2]["model"] == "claude-sonnet-4-6"


def test_live_falls_back_to_second_model(live):
    conn = live([RuntimeError("model unavailable"), {"headline": "H", "bullets": ["x", "y", "z"]}])
    b = asyncio.run(briefing.call_brief(_flags(), INFO, run_id="run-2"))
    assert b.status == "done" and b.model == "llama3.3-70b"
    assert [c[2]["model"] for c in conn.calls if c[0] == "execute" and c[2]] == ["claude-sonnet-4-6", "llama3.3-70b"]


def test_live_failure_never_raises(live):
    live([RuntimeError("boom"), RuntimeError("boom again")])
    b = asyncio.run(briefing.call_brief(_flags(), INFO))
    assert b.status == "failed" and "AI_COMPLETE failed" in b.error


def test_live_timeout_is_reported(live, monkeypatch):
    monkeypatch.setattr(briefing, "CAP_S", 0.3)
    live([{"headline": "H", "bullets": ["x"]}], sleep=1.0)
    b = asyncio.run(briefing.call_brief(_flags(), INFO))
    assert b.status == "failed" and b.error.startswith("timed out")
