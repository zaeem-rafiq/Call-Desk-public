"""Replay a recorded run (data/replay/latest/events.jsonl) with its original timing."""

from __future__ import annotations

import asyncio
import json

from pathlib import Path

from . import config
from .config import REPLAY_DIR
from .hub import EventHub

LATEST = REPLAY_DIR / "latest"  # the default call's cache (PepsiCo)
PREFILL_S = 24.0  # transcript seconds shown at once when a replay starts mid-call


def latest_dir(call_id: str | None = None) -> Path:
    """Replay cache for a call: data/replay/latest for the default call, latest-<call_id> for others."""
    cid = call_id or config.active_call_id()
    return LATEST if cid == config.DEFAULT_CALL else REPLAY_DIR / f"latest-{cid}"


def available(call_id: str | None = None) -> bool:
    return (latest_dir(call_id) / "events.jsonl").exists()


def meta(call_id: str | None = None) -> dict:
    path = latest_dir(call_id) / "meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


async def run_replay(hub: EventHub, speed: float = 1.0, start_s: float = 0.0) -> dict:
    """Re-publish a recorded run. Recorded t is seconds since that run started (= audio time
    for a real-time run that started at 0), so start_s skips ahead in the same clock."""
    events = [json.loads(line) for line in (latest_dir() / "events.jsonl").read_text().splitlines() if line.strip()]
    rec_start = next((e["data"].get("start_s", 0.0) for e in events if e["event"] == "status"), 0.0)
    skip = max(0.0, start_s - rec_start)
    t0 = hub.elapsed()
    for item in events:
        if item["event"] == "reset":
            continue
        is_status = item["event"] == "status"
        if item["t"] < skip and not is_status and not (item["event"] == "transcript" and item["data"].get("t", -1) >= rec_start + skip - PREFILL_S):
            continue
        # A flag or transcript line spoken before the start point stays out, even if it was updated later
        # (a late enrichment of an earlier card would otherwise pop onto the top of the feed).
        spoken = item["data"].get("t_offset") if item["event"] == "flag" else item["data"].get("t") if item["event"] == "transcript" else None
        if spoken is not None and spoken < rec_start + skip:
            if item["event"] == "transcript" and spoken >= rec_start + skip - PREFILL_S:
                hub.publish("transcript", item["data"])  # recent lines fill the pane at once, like a live mid-call start
            continue
        if speed > 0 and not is_status:
            await asyncio.sleep(max(0.0, (item["t"] - skip) / speed - (hub.elapsed() - t0)))
        data = item["data"]
        if is_status:
            data = {**data, "mode": "replay", "recorded_run": meta().get("run_id"), "start_s": rec_start + skip}
        hub.publish(item["event"], data)
    return {"state": "done", "mode": "replay"}
