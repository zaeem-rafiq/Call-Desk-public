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


async def run_replay(hub: EventHub, speed: float = 1.0) -> dict:
    events = [json.loads(line) for line in (LATEST / "events.jsonl").read_text().splitlines() if line.strip()]
    t0 = hub.elapsed()
    for item in events:
        if item["event"] == "reset":
            continue
        if speed > 0:
            await asyncio.sleep(max(0.0, item["t"] / speed - (hub.elapsed() - t0)))
        data = item["data"]
        if item["event"] == "status":
            data = {**data, "mode": "replay", "recorded_run": meta().get("run_id")}
        hub.publish(item["event"], data)
    return {"state": "done", "mode": "replay"}
