"""FastAPI app: static terminal UI, SSE feed, and run control.

Run:  .venv/bin/python -m app.main            (live pipeline, mocks where keys are missing)
      .venv/bin/python -m app.main --replay   (play back data/replay/latest)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, observability, pipeline, replay
from .hub import EventHub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("calldesk")

observability.init_sentry()
app = FastAPI(title="Call Desk")
hub = EventHub()
DEFAULT_MODE = "live"


class RunManager:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.mode: str | None = None
        self.start_s = 0.0
        self.speed = 1.0

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, mode: str, speed: float, start_s: float = 0.0, call_id: str | None = None) -> str:
        await self.stop()  # the old run must finish publishing "stopped" before the new run's hub reset
        if call_id:
            config.set_active_call(call_id)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        record = mode == "live"
        hub.start_run(run_id, record=record)
        chunk = float(config.get("CHUNK_SECONDS") or 12)
        self.mode, self.speed = mode, speed
        self.start_s = (start_s // chunk) * chunk if mode == "live" else start_s  # live runs snap to a chunk
        self.task = asyncio.create_task(self._run(mode, speed, run_id, record, start_s))
        return run_id

    async def _run(self, mode: str, speed: float, run_id: str, record: bool, start_s: float) -> None:
        try:
            summary = await (pipeline.run_live(hub, speed, start_s) if mode == "live" else replay.run_replay(hub, speed, start_s))
            if record:
                _promote(run_id, {**summary, "speed": speed, "start_s": start_s})
        except asyncio.CancelledError:
            hub.publish("status", {"state": "stopped", "mode": mode})
            hub.close_recording()
            raise
        except Exception as exc:  # noqa: BLE001
            observability.capture(exc)
            hub.publish("status", {"state": "error", "mode": mode, "message": f"{type(exc).__name__}: {exc}"})
            hub.close_recording()

    async def stop(self) -> None:
        if self.running:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task


def _run_quality(summary: dict) -> int | None:
    """Number of live integrations for a clean, full, real-time Gemini run; None if not replay-worthy."""
    clean = (
        summary.get("transcriber") == "gemini"
        and summary.get("speed") == 1.0
        and summary.get("start_s", 0) == 0
        and not summary.get("errors")
        and not summary.get("degraded")
    )
    return sum(v == "live" for v in summary.get("integrations", {}).values()) if clean else None


def _promote(run_id: str, summary: dict) -> None:
    """Replace the replay cache only with a clean run at least as live as the cached one."""
    if config.get("REPLAY_FREEZE") == "1":  # demo day: rehearsals never replace the stage fallback
        hub.finish_run(promote=False)
        log.info("REPLAY_FREEZE=1: run %s kept out of the replay cache", run_id)
        return
    quality = _run_quality(summary)
    cached = replay.meta()
    cached_quality = _run_quality(cached) if cached else None
    if replay.available() and (quality is None or (cached_quality is not None and quality < cached_quality)):
        hub.finish_run(promote=False)
        log.info("run %s kept out of the replay cache (quality %s vs cached %s)", run_id, quality, cached_quality)
        return
    latest = hub.finish_run(promote=True, target=replay.latest_dir())
    if latest:
        (latest / "meta.json").write_text(json.dumps({"run_id": run_id, **summary}, indent=2))
        log.info("replay cache updated from run %s (quality %s)", run_id, quality)


runs = RunManager()


class RunRequest(BaseModel):
    mode: str | None = None  # live | replay
    speed: float = 1.0  # 1.0 = real time, 0 = as fast as possible
    start_s: float = 0.0  # start this many seconds into the clip (snapped to a chunk boundary)
    call: str | None = None  # call id from /api/state "calls"; default keeps the active call


@app.post("/api/run")
async def start_run(req: RunRequest) -> dict:
    mode = req.mode or DEFAULT_MODE
    if req.call and req.call not in {c["id"] for c in config.list_calls()}:
        raise HTTPException(404, f"unknown call {req.call!r}")
    if mode == "replay" and not replay.available(req.call):
        raise HTTPException(409, "no replay cache yet for this call; run live once first")
    if mode not in ("live", "replay"):
        raise HTTPException(400, "mode must be live or replay")
    run_id = await runs.start(mode, req.speed, max(0.0, req.start_s), req.call)
    return {"run_id": run_id, "mode": mode, "call": config.active_call_id()}


@app.post("/api/stop")
async def stop_run() -> dict:
    await runs.stop()
    return {"stopped": True}


@app.get("/api/state")
async def state(call: str | None = None) -> dict:
    """State for the active call, or for ?call=<id> (a preview for the call picker; does not switch)."""
    if call and call not in {c["id"] for c in config.list_calls()}:
        raise HTTPException(404, f"unknown call {call!r}")
    info = config.call_info(call)
    return {
        "default_mode": DEFAULT_MODE,
        "running": runs.running,
        "mode": runs.mode,
        "position_s": round(runs.start_s + hub.elapsed() * runs.speed, 2) if runs.running else None,
        "speed": runs.speed,
        "integrations": config.integration_modes(),
        "replay_available": replay.available(info.call_id),
        "audio": info.audio_path.exists(),
        "audio_url": f"/audio/{info.call_id}.mp3",
        "active_call": config.active_call_id(),
        "calls": config.list_calls(),
        "call": {"id": info.call_id, "title": info.title, "company": info.company, "ticker": info.ticker,
                 "benchmark": info.primary_benchmark, "benchmarks": list(info.benchmarks),
                 "session_day": info.session_day.isoformat(), "clip_start": info.clip_start.isoformat(timespec="seconds")},
        "sentry": observability.enabled(),
        "sentry_browser": getattr(observability, "browser_config", lambda: None)(),
    }


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    async def stream():
        q = hub.subscribe()
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {item['event']}\ndata: {json.dumps({'t': item['t'], **item['data']})}\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/audio/call.mp3")
async def call_audio() -> FileResponse:
    """Audio of the active call."""
    return await call_audio_for(config.active_call_id())


@app.get("/audio/{call_id}.mp3")
async def call_audio_for(call_id: str) -> FileResponse:
    if call_id not in {c["id"] for c in config.list_calls()}:
        raise HTTPException(404, f"unknown call {call_id!r}")
    path = config.call_info(call_id).audio_path
    if not path.exists():
        raise HTTPException(404, f"{path.name} not built yet (scripts/fetch_audio.py)")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/export.md")
async def export_note() -> PlainTextResponse:
    """The current run's flags as a Markdown morning note (live or replay)."""
    from . import export

    return PlainTextResponse(export.morning_note(hub.history), media_type="text/markdown; charset=utf-8")


@app.get("/api/debug/sentry")
async def sentry_check() -> dict:
    """Send one test error so you can confirm Sentry receives events."""
    try:
        raise RuntimeError("Call Desk Sentry test event")
    except RuntimeError as exc:
        observability.capture(exc)
    return {"sent": observability.enabled()}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.STATIC / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC), name="static")


def cli() -> None:
    global DEFAULT_MODE
    parser = argparse.ArgumentParser(description="Call Desk server")
    parser.add_argument("--replay", action="store_true", help="default the Play button to the cached replay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    DEFAULT_MODE = "replay" if args.replay else "live"
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    cli()
