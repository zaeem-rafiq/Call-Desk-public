"""Sentry: browser config served to the page, latency measurements on each flag's trace, and the self-check.

All fakes, no network. conftest.py forces MOCK_SENTRY=1; tests that need the live path patch ensure_started.
"""

import asyncio
import re
from contextlib import contextmanager

import sentry_sdk

from app import config, observability
from app.claims import Claim
from app.models import Flag


class FakeTxn:
    def __init__(self):
        self.tags, self.measurements, self.data = {}, {}, {}

    def set_tag(self, k, v):
        self.tags[k] = v

    def set_measurement(self, name, value, unit=""):
        self.measurements[name] = (value, unit)

    def set_data(self, k, v):
        self.data[k] = v


def _flag(**kw):
    base = dict(id="f01-core_eps_growth", call_ts="2026-10-08T08:20:17-04:00", t_offset=147.2, speaker="CFO", quote="q",
                claim_type="guidance", metric="core_eps_growth", metric_label="Core EPS growth", direction="cut")
    return Flag(**{**base, **kw})


def _live(monkeypatch, dsn="https://pubkey@o1.ingest.sentry.io/42"):
    monkeypatch.setattr(observability, "ensure_started", lambda: True)
    monkeypatch.setenv("SENTRY_DSN", dsn)
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "demo")
    monkeypatch.setenv("SENTRY_ORG", "acme")


def test_browser_config_is_none_while_sentry_is_off(monkeypatch):
    monkeypatch.setattr(observability, "_enabled", False)
    assert observability.browser_config() is None


def test_browser_config_serves_only_the_public_key(monkeypatch):
    _live(monkeypatch)
    assert observability.browser_config() == {"dsn": "https://pubkey@o1.ingest.sentry.io/42", "environment": "demo",
                                              "release": observability.RELEASE}
    _live(monkeypatch, dsn="https://pubkey:legacysecret@o1.ingest.sentry.io:443/42")
    cfg = observability.browser_config()
    assert cfg["dsn"] == "https://pubkey@o1.ingest.sentry.io:443/42" and "legacysecret" not in str(cfg)
    _live(monkeypatch, dsn="not a dsn")
    assert observability.browser_config() is None


def test_record_verdict_puts_both_latencies_on_the_transaction():
    txn = FakeTxn()
    observability.record_verdict(txn, _flag(latency_s=4.1, checked_latency_s=19.6))
    assert txn.measurements == {"quote_to_flag_s": (4.1, "second"), "quote_to_verdict_s": (19.6, "second")}
    assert txn.tags == {"calldesk.metric": "core_eps_growth", "calldesk.direction": "cut"}
    assert txn.data == {"calldesk.quote_to_flag_s": 4.1, "calldesk.quote_to_verdict_s": 19.6}


def test_record_verdict_skips_missing_latencies_and_never_raises():
    txn = FakeTxn()
    observability.record_verdict(txn, _flag())  # no audio clock (replay, scripts): tags only
    assert txn.measurements == {} and txn.tags["calldesk.metric"] == "core_eps_growth"

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("sentry down")
    observability.record_verdict(Broken(), _flag(latency_s=1.0))  # must not raise


def test_fact_check_records_the_verdict_latency_on_its_own_trace(monkeypatch):
    for name in config.INTEGRATIONS:
        monkeypatch.setenv(f"MOCK_{name.upper()}", "1")
    monkeypatch.setenv("ENRICH", "0")
    from app import factcheck

    txn = FakeTxn()

    @contextmanager
    def fake_transaction(name, data=None, agent="call-desk-fact-checker"):
        yield "a" * 32, txn
    monkeypatch.setattr(observability, "agent_transaction", fake_transaction)

    def publish(f):  # what pipeline._safe_check's publish does on the first "checked" publish
        if f.status == "checked" and f.checked_latency_s is None:
            f.checked_latency_s = 21.5

    claim = Claim(quote="it's the margin piece that's driving the guide down in EPS", speaker="CFO", claim_type="guidance",
                  metric="core_eps_growth", direction="cut", materiality=0.9)
    flag = _flag(quote=claim.quote, latency_s=3.0)
    out = asyncio.run(factcheck.fact_check(claim, flag, config.call_info(), publish))
    assert out.status == "checked" and out.sentry_trace_id == "a" * 32
    assert txn.measurements == {"quote_to_flag_s": (3.0, "second"), "quote_to_verdict_s": (21.5, "second")}
    assert txn.tags["calldesk.direction"] == out.direction


def test_self_check_reports_sentry_off_without_raising(monkeypatch):
    monkeypatch.setattr(observability, "_enabled", False)
    rows = asyncio.run(observability.self_check())
    assert rows == [("Sentry started", False, "off: MOCK_SENTRY=1")]


def test_self_check_live_path_sends_one_trace(monkeypatch):
    assert not sentry_sdk.get_client().is_active()  # no real client in tests: the test transaction goes nowhere
    _live(monkeypatch)
    flushes = []
    monkeypatch.setattr(sentry_sdk, "flush", lambda timeout=None, callback=None: flushes.append(timeout))
    rows = asyncio.run(observability.self_check())
    by = {name: (ok, detail) for name, ok, detail in rows}
    assert [name for name, _, _ in rows] == ["Sentry started", "browser config served", "test trace sent", "trace URL format"]
    assert all(ok for ok, _ in by.values()), rows
    assert flushes == [5.0]
    assert re.fullmatch(r"https://acme\.sentry\.io/explore/traces/trace/[0-9a-f]{32}/", by["trace URL format"][1])
    assert "pubkey" not in str(rows)  # the report never shows the DSN


def test_browser_bundle_is_pinned_with_sri():
    js = (config.STATIC / "features" / "sentry.js").read_text()
    version = re.search(r'SDK_VERSION = "([^"]+)"', js).group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)  # an exact release, never "latest"
    assert re.search(r'SDK_INTEGRITY = "sha384-[A-Za-z0-9+/]{64}"', js)
    assert 'crossOrigin = "anonymous"' in js and "innerHTML" not in js
