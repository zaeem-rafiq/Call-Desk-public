"""Sentry error monitoring and AI agent tracing.

Each fact-check runs in its own Sentry transaction (one trace per flag). LLM
calls and tool calls are child spans using Sentry's gen_ai span conventions.
When SENTRY_DSN is missing the same code runs, the spans go nowhere, and the
flag still carries a locally generated trace id.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import sentry_sdk

from . import config

log = logging.getLogger("calldesk")
_enabled = False
_init_lock = threading.Lock()


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
        environment=config.get("SENTRY_ENVIRONMENT", "hackathon"),
        release="call-desk@0.1.0",
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


@contextmanager
def span(op: str, name: str, data: dict[str, Any] | None = None) -> Iterator[Any]:
    with sentry_sdk.start_span(op=op, name=name) as s:
        for k, v in (data or {}).items():
            s.set_data(k, v)
        yield s


def capture(exc: BaseException) -> None:
    log.warning("captured error: %r", exc)
    sentry_sdk.capture_exception(exc)
