"""Sentry error monitoring and AI agent tracing.

Each fact-check runs in its own Sentry transaction (one trace per flag). LLM
calls and tool calls are child spans using Sentry's gen_ai span conventions.
When SENTRY_DSN is missing the same code runs, the spans go nowhere, and the
flag still carries a locally generated trace id.

The browser SDK (static/features/sentry.js) gets its settings from browser_config() via /api/state, joins
page-load traces to the server's request traces, and adds a "ui.render" span to each flag's own trace.
self_check() -> list of (name, ok, detail) rows for scripts/verify_live.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import sentry_sdk

from . import config

log = logging.getLogger("calldesk")
_enabled = False
_init_lock = threading.Lock()
RELEASE = "call-desk@0.1.0"  # shared by the server and browser SDKs so their events group under one release


def _environment() -> str:
    return config.get("SENTRY_ENVIRONMENT") or "hackathon"


def init_sentry() -> bool:
    global _enabled
    if config.is_mock("sentry"):
        log.info("Sentry: MOCK (%s)", config.mock_reason("sentry"))
        return False
    integrations: list[Any] = []
    try:  # Auto-instruments google-genai calls as gen_ai spans when available.
        from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration

        integrations.append(GoogleGenAIIntegration(include_prompts=True))
    except Exception:  # pragma: no cover - integration missing in this SDK version
        pass
    sentry_sdk.init(
        dsn=config.get("SENTRY_DSN"),
        environment=_environment(),
        release=RELEASE,
        traces_sample_rate=1.0,
        send_default_pii=True,  # needed for prompts/responses on gen_ai spans; no end-user PII here
        integrations=integrations,
    )
    _enabled = True
    log.info("Sentry: LIVE")
    return True


def enabled() -> bool:
    return _enabled


def ensure_started() -> bool:
    """Start Sentry the first time its DSN is present.

    init_sentry() also runs at server start; this covers a key added to .env while the server runs, which
    otherwise left Sentry off (no trace links on any card) until a restart.
    """
    if _enabled:
        return True
    if config.is_mock("sentry"):
        return False
    with _init_lock:
        return _enabled or init_sentry()


def trace_url(trace_id: str | None) -> str | None:
    org = config.get("SENTRY_ORG")
    if not (ensure_started() and trace_id and org):
        return None
    return f"https://{org}.sentry.io/explore/traces/trace/{trace_id}/"


def _public_dsn(dsn: str) -> str | None:
    """The DSN without a legacy secret key (``key:secret@``). Only the public key may reach a browser."""
    try:
        parts = urlsplit(dsn)
        if not (parts.scheme and parts.hostname and parts.username):
            return None
        host = f"{parts.username}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return None


def browser_config() -> dict[str, str] | None:
    """Settings for the Sentry browser SDK, served in /api/state. None while Sentry is off (mock or no DSN)."""
    if not ensure_started():
        return None
    dsn = _public_dsn(config.get("SENTRY_DSN") or "")
    return {"dsn": dsn, "environment": _environment(), "release": RELEASE} if dsn else None


@contextmanager
def agent_transaction(name: str, data: dict[str, Any] | None = None, agent: str = "call-desk-fact-checker") -> Iterator[tuple[str, Any]]:
    """Start a fresh trace (one per flag or audio chunk). Yields (trace_id, transaction)."""
    trace_id = uuid.uuid4().hex
    ensure_started()
    with sentry_sdk.isolation_scope() as scope:
        scope.set_tag("calldesk.agent", agent)
        with sentry_sdk.start_transaction(op="gen_ai.invoke_agent", name=name, trace_id=trace_id, sampled=True) as txn:
            txn.set_data("gen_ai.operation.name", "invoke_agent")
            txn.set_data("gen_ai.agent.name", agent)
            for k, v in (data or {}).items():
                txn.set_data(k, v)
            yield trace_id, txn


def record_verdict(txn: Any, flag: Any) -> None:
    """Put a flag's headline latencies on its fact-check transaction, right after the verdict. Never raises.

    quote_to_flag_s: audio seconds from the quote to the flag card; quote_to_verdict_s: ... to the verdict.
    Measurements make them chartable in Sentry; the same values are also span data for the newer span APIs.
    """
    try:
        txn.set_tag("calldesk.metric", flag.metric)
        txn.set_tag("calldesk.direction", flag.direction or "n/a")
        for name, value in (("quote_to_flag_s", flag.latency_s), ("quote_to_verdict_s", flag.checked_latency_s)):
            if value is None:
                continue
            with warnings.catch_warnings():  # set_measurement is deprecated in 2.x but is still how transactions carry them
                warnings.simplefilter("ignore", DeprecationWarning)
                txn.set_measurement(name, float(value), "second")
            txn.set_data(f"calldesk.{name}", float(value))
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a fact-check
        log.warning("sentry measurements skipped for %s: %r", getattr(flag, "id", "?"), exc)


@contextmanager
def span(op: str, name: str, data: dict[str, Any] | None = None) -> Iterator[Any]:
    with sentry_sdk.start_span(op=op, name=name) as s:
        for k, v in (data or {}).items():
            s.set_data(k, v)
        yield s


def capture(exc: BaseException) -> None:
    log.warning("captured error: %r", exc)
    sentry_sdk.capture_exception(exc)


async def self_check() -> list[tuple[str, bool, str]]:
    """Live check for scripts/verify_live.py: Sentry on, browser config ready, one test trace sent. Never raises."""
    t0 = time.monotonic()
    rows: list[tuple[str, bool, str]] = []
    try:
        started = ensure_started()
    except Exception as exc:  # noqa: BLE001
        return [("Sentry started", False, f"{type(exc).__name__}: {exc}"[:120])]
    rows.append(("Sentry started", started, "live" if started else f"off: {config.mock_reason('sentry') or 'init failed'}"))
    if not started:
        return rows

    cfg = browser_config() or {}
    rows.append(("browser config served", all(cfg.get(k) for k in ("dsn", "environment", "release")),
                 f"environment={cfg.get('environment')} release={cfg.get('release')} dsn={'set' if cfg.get('dsn') else 'missing'}"))

    trace_id = uuid.uuid4().hex
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_tag("calldesk.agent", "self-check")
            with sentry_sdk.start_transaction(op="calldesk.self_check", name="call-desk self-check", trace_id=trace_id,
                                              sampled=True) as txn:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    txn.set_measurement("startup_ms", round((time.monotonic() - t0) * 1000, 1), "millisecond")
        flushed = time.monotonic()
        await asyncio.wait_for(asyncio.to_thread(sentry_sdk.flush, 5.0), timeout=6)
        took = time.monotonic() - flushed
        rows.append(("test trace sent", took < 4.9, f"flushed in {took * 1000:.0f} ms" + ("" if took < 4.9 else " (flush timed out)")))
    except Exception as exc:  # noqa: BLE001
        rows.append(("test trace sent", False, f"{type(exc).__name__}: {exc}"[:120]))

    org = config.get("SENTRY_ORG") or ""
    url = trace_url(trace_id)
    pattern = rf"https://{re.escape(org)}\.sentry\.io/explore/traces/trace/{trace_id}/"
    rows.append(("trace URL format", bool(org and url and re.fullmatch(pattern, url)), url or "SENTRY_ORG missing"))
    return rows
