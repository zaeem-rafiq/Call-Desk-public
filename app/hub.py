"""In-process pub/sub for Server-Sent Events, with history and a run recorder.

Every published event is kept in memory (so a browser that connects mid-run
catches up) and appended to data/replay/runs/<run_id>/events.jsonl with its
time offset, which is what --replay plays back.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .config import REPLAY_DIR


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self.history: list[dict[str, Any]] = []
        self.run_id: str | None = None
        self._t0 = 0.0
        self._record_file = None
        self._record_dir: Path | None = None

    # ---- run lifecycle -------------------------------------------------
    def start_run(self, run_id: str, record: bool = True) -> None:
        self.close_recording()
        self.run_id = run_id
        self.history = []
        self._t0 = time.monotonic()
        if record:
            self._record_dir = REPLAY_DIR / "runs" / run_id
            self._record_dir.mkdir(parents=True, exist_ok=True)
            self._record_file = (self._record_dir / "events.jsonl").open("w")
        self.publish("reset", {"run_id": run_id})

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def close_recording(self) -> None:
        if self._record_file:
            self._record_file.close()
            self._record_file = None

    def finish_run(self, promote: bool, target: Path | None = None) -> Path | None:
        """Close the recording; when promote, copy it to target (default data/replay/latest)."""
        self.close_recording()
        if not (promote and self._record_dir):
            return None
        latest = target or REPLAY_DIR / "latest"
        prev = latest.with_name(latest.name + "-prev")
        if latest.exists():
            if prev.exists():
                shutil.rmtree(prev)
            latest.rename(prev)  # keep the previous good cache one step back
        shutil.copytree(self._record_dir, latest)
        _write_readable_exports(latest)
        return latest

    # ---- pub/sub --------------------------------------------------------
    def publish(self, event: str, data: dict[str, Any]) -> None:
        item = {"event": event, "t": round(self.elapsed(), 3), "data": data}
        self.history.append(item)
        if self._record_file:
            self._record_file.write(json.dumps(item) + "\n")
            self._record_file.flush()
        for q in list(self._subscribers):
            q.put_nowait(item)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for item in self.history:
            q.put_nowait(item)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)


def _write_readable_exports(run_dir: Path) -> None:
    """Write flags.jsonl (final state per flag) and transcript.txt next to events.jsonl."""
    flags: dict[str, dict] = {}
    lines: list[str] = []
    for raw in (run_dir / "events.jsonl").read_text().splitlines():
        item = json.loads(raw)
        if item["event"] == "flag":
            flags[item["data"]["id"]] = {"t": item["t"], **item["data"]}
        elif item["event"] == "transcript":
            d = item["data"]
            lines.append(f"[{d['t']:7.1f}s] {d['speaker']}: {d['text']}")
    (run_dir / "flags.jsonl").write_text("".join(json.dumps(f) + "\n" for f in flags.values()))
    (run_dir / "transcript.txt").write_text("\n".join(lines) + "\n")
