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
    "LISTENER": "chunks",  # chunks | live (Gemini Live streaming transcription)
    "CALL_ID": "",  # default call; empty means pep_q3_2026
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
    """One earnings call. Built from data/calls/<call_id>/call.json (see list_calls())."""

    call_id: str
    company: str
    ticker: str
    benchmarks: tuple[str, ...]
    primary_benchmark: str
    title: str
    call_start: datetime
    clip_start_offset_s: float  # offset of the clip into the live call
    audio_path: Path
    roster: tuple[str, ...] = ()  # "Name (Role)" for executives
    analysts: tuple[str, ...] = ()  # "Name (Firm)" for analysts the clip may greet by first name
    peers: tuple[str, ...] = ()  # peer tickers for the read-across panel
    call_context: str = ""  # one sentence for the agents: format of the call and when the release came out
    ir_domains: tuple[str, ...] = ()  # official sites for the company's release and remarks
    prices_path: Path = DATA / "prices.csv"
    prior_close_path: Path = DATA / "prior_close.csv"
    day_close_path: Path = DATA / "day_close.csv"
    prior_dir: Path = DATA / "prior"
    golden_path: Path = ROOT / "tests" / "golden.json"

    @property
    def clip_start(self) -> datetime:
        return self.call_start + timedelta(seconds=self.clip_start_offset_s)

    @property
    def session_day(self):
        return self.call_start.date()

    def call_ts(self, t_offset_s: float) -> datetime:
        """Map seconds into the clip to the real call time (ET)."""
        return self.clip_start + timedelta(seconds=t_offset_s)


CALLS_DIR = DATA / "calls"
DEFAULT_CALL = "pep_q3_2026"
_active_call: str | None = None


def list_calls() -> list[dict]:
    """Calls with a call.json whose audio exists (or can be rebuilt), for the call picker."""
    out = []
    for path in sorted(CALLS_DIR.glob("*/call.json")):
        spec = json.loads(path.read_text())
        out.append({"id": path.parent.name, "title": spec["title"], "ticker": spec["ticker"], "company": spec["company"],
                    "audio": (ROOT / spec["audio"]).exists()})
    return out


def set_active_call(call_id: str) -> None:
    global _active_call
    if not (CALLS_DIR / call_id / "call.json").exists():
        raise ValueError(f"unknown call {call_id!r}")
    _active_call = call_id


def active_call_id() -> str:
    return _active_call or get("CALL_ID") or DEFAULT_CALL


def call_info(call_id: str | None = None) -> CallInfo:
    """The active call (set per run), or a named one."""
    cid = call_id or active_call_id()
    spec = json.loads((CALLS_DIR / cid / "call.json").read_text())
    meta_path = ROOT / spec["meta"]
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    call_start = datetime.fromisoformat(spec["call_start_et"])
    offset = 0.0
    if meta.get("estimated_clip_start_et"):
        clip_start = datetime.fromisoformat(meta["estimated_clip_start_et"])
        if clip_start.tzinfo is None:
            clip_start = clip_start.replace(tzinfo=ET)
        offset = (clip_start - call_start).total_seconds()
    audio = get("CALL_AUDIO") if cid == DEFAULT_CALL and get("CALL_AUDIO") != DEFAULTS["CALL_AUDIO"] else spec["audio"]
    return CallInfo(
        call_id=cid,
        company=spec["company"],
        ticker=spec["ticker"],
        benchmarks=tuple(spec["benchmarks"]),
        primary_benchmark=spec["benchmarks"][0],
        title=spec["title"],
        call_start=call_start,
        clip_start_offset_s=offset,
        audio_path=ROOT / audio,
        roster=tuple(spec.get("roster", [])),
        analysts=tuple(spec.get("analysts", [])),
        peers=tuple(spec.get("peers", [])),
        call_context=spec.get("call_context", ""),
        ir_domains=tuple(spec.get("ir_domains", [])),
        prices_path=ROOT / spec["prices"],
        prior_close_path=ROOT / spec["prior_close"],
        day_close_path=ROOT / spec["day_close"],
        prior_dir=ROOT / spec["prior_dir"],
        golden_path=ROOT / spec["golden"],
    )
