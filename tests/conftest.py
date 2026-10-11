"""Shared test setup: tests never send events to the real Sentry project, even when .env has a DSN."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_sentry(monkeypatch):
    monkeypatch.setenv("MOCK_SENTRY", "1")
