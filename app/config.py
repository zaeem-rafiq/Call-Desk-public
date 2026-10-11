"""Settings, call metadata, and live-vs-mock decisions.

Settings are re-read from .env on every call so keys added while the server
runs take effect on the next run (Sentry is the exception: it initializes once
at startup).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
REPLAY_DIR = DATA / "replay"
ET = ZoneInfo("America/New_York")

# Which env vars each integration needs to run live.
INTEGRATIONS: dict[str, list[str]] = {
    "gemini": ["GEMINI_API_KEY"],
    "nimble": ["NIMBLE_API_KEY"],
    "tiger": ["TIGER_DATABASE_URL"],
    "snowflake": ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"],
    "sentry": ["SENTRY_DSN"],
}

DEFAULTS = {
    "SNOWFLAKE_WAREHOUSE": "COMPUTE_WH",
    "SNOWFLAKE_DATABASE": "CALLDESK",
    "SNOWFLAKE_SCHEMA": "PRIOR",
    # Model IDs are configurable; defaults come from docs/sdk_notes.md.
    "GEMINI_MODEL_FAST": "gemini-3.8-flash",
    "GEMINI_MODEL_AGENT": "gemini-3.8-flash",
    "GEMINI_MODEL_TTS": "gemini-3.8-flash-tts",
    "CHUNK_SECONDS": "12",
    "MATERIALITY_THRESHOLD": "0.6",
    "REACTION_WINDOW_MIN": "30",
    "CALL_AUDIO": "data/call.mp3",
}


_ENV_PREFIXES = ("MOCK_", "GEMINI_", "NIMBLE_", "TIGER_", "SNOWFLAKE_", "SENTRY_")


def env() -> dict[str, str]:
    """Defaults < .env < process env. Never log the result: it holds secrets."""
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in dotenv_values(ROOT / ".env").items() if v})
    merged.update({k: v for k, v in os.environ.items() if v and (k in DEFAULTS or k.startswith(_ENV_PREFIXES))})
    return merged


def get(name: str, default: str | None = None) -> str | None:
    return env().get(name, default)


def mock_reason(integration: str) -> str | None:
    """None when the integration should run live, else a human-readable reason."""
    e = env()
    if e.get(f"MOCK_{integration.upper()}", "0") == "1":
        return f"MOCK_{integration.upper()}=1"
    missing = [v for v in INTEGRATIONS[integration] if not e.get(v)]
    if missing:
        return "missing " + ", ".join(missing)
    return None


def is_mock(integration: str) -> bool:
    return mock_reason(integration) is not None


def integration_modes() -> dict[str, str]:
    return {name: ("mock" if is_mock(name) else "live") for name in INTEGRATIONS}


@dataclass(frozen=True)
class CallInfo:
    company: str
    ticker: str
    benchmarks: tuple[str, ...]
    primary_benchmark: str
    title: str
    call_start: datetime
    clip_start_offset_s: float  # offset of data/call.mp3 into the live call
    audio_path: Path

    @property
    def clip_start(self) -> datetime:
        return self.call_start + timedelta(seconds=self.clip_start_offset_s)

    def call_ts(self, t_offset_s: float) -> datetime:
        """Map seconds into the clip to the real call time (ET)."""
        return self.clip_start + timedelta(seconds=t_offset_s)


def call_info() -> CallInfo:
    meta_path = DATA / "call_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    call_start = datetime(2026, 10, 8, 8, 15, tzinfo=ET)
    offset = 0.0
    if meta.get("estimated_clip_start_et"):
        clip_start = datetime.fromisoformat(meta["estimated_clip_start_et"])
        if clip_start.tzinfo is None:
            clip_start = clip_start.replace(tzinfo=ET)
        offset = (clip_start - call_start).total_seconds()
    return CallInfo(
        company="PepsiCo",
        ticker="PEP",
        benchmarks=("XLP", "SPY"),
        primary_benchmark="XLP",
        title="PepsiCo Q3 2026 Earnings Call · Q&A",
        call_start=call_start,
        clip_start_offset_s=offset,
        audio_path=ROOT / (get("CALL_AUDIO") or DEFAULTS["CALL_AUDIO"]),
    )
