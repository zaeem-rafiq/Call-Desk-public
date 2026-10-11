"""Replay a recorded run (data/replay/latest/events.jsonl) with its original timing."""

from __future__ import annotations

import asyncio
import json

from .config import REPLAY_DIR
from .hub import EventHub

LATEST = REPLAY_DIR / "latest"


def available() -> bool:
    return (LATEST / "events.jsonl").exists()


def meta() -> dict:
    path = LATEST / "meta.json"
    return json.loads(path.read_text()) if path.exists() else {}


async def run_replay(hub: EventHub, speed: float = 1.0, start_s: float = 0.0) -> dict:
    """Re-publish a recorded run. Recorded t is seconds since that run started (= audio time
    for a real-time run that started at 0), so start_s skips ahead in the same clock."""
    events = [json.loads(line) for line in (LATEST / "events.jsonl").read_text().splitlines() if line.strip()]
    rec_start = next((e["data"].get("start_s", 0.0) for e in events if e["event"] == "status"), 0.0)
    skip = max(0.0, start_s - rec_start)
    t0 = hub.elapsed()
    for item in events:
        if item["event"] == "reset":
            continue
        is_status = item["event"] == "status"
        if item["t"] < skip and not is_status:
            continue
        if speed > 0 and not is_status:
            await asyncio.sleep(max(0.0, (item["t"] - skip) / speed - (hub.elapsed() - t0)))
        data = item["data"]
        if is_status:
            data = {**data, "mode": "replay", "recorded_run": meta().get("run_id"), "start_s": rec_start + skip}
        hub.publish(item["event"], data)
    return {"state": "done", "mode": "replay"}
