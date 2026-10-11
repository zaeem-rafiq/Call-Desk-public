"""FastAPI app: static terminal UI, SSE feed, and run control.

Run:  .venv/bin/python -m app.main            (live pipeline, mocks where keys are missing)
      .venv/bin/python -m app.main --replay   (play back data/replay/latest)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
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

    def start(self, mode: str, speed: float, start_s: float = 0.0) -> str:
        self.stop()
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        record = mode == "live"
        hub.start_run(run_id, record=record)
        self.mode, self.start_s, self.speed = mode, start_s, speed
        self.task = asyncio.create_task(self._run(mode, speed, run_id, record, start_s))
        return run_id

    async def _run(self, mode: str, speed: float, run_id: str, record: bool, start_s: float) -> None:
        try:
            summary = await (pipeline.run_live(hub, speed, start_s) if mode == "live" else replay.run_replay(hub, speed, start_s))
            if record and start_s == 0:
                _promote(run_id, summary)
        except asyncio.CancelledError:
            hub.publish("status", {"state": "stopped", "mode": mode})
            hub.close_recording()
            raise
        except Exception as exc:  # noqa: BLE001
            observability.capture(exc)
            hub.publish("status", {"state": "error", "mode": mode, "message": f"{type(exc).__name__}: {exc}"})
            hub.close_recording()

    def stop(self) -> None:
        if self.running:
            self.task.cancel()


def _promote(run_id: str, summary: dict) -> None:
    """Keep the best run as the replay cache: a real-Gemini run, or anything if no cache exists."""
    gemini_live = summary.get("transcriber") == "gemini"
    if not (gemini_live or not replay.available()):
        hub.finish_run(promote=False)
        return
    latest = hub.finish_run(promote=True)
    if latest:
        (latest / "meta.json").write_text(json.dumps({"run_id": run_id, **summary}, indent=2))
        log.info("replay cache updated from run %s", run_id)


runs = RunManager()


class RunRequest(BaseModel):
    mode: str | None = None  # live | replay
    speed: float = 1.0  # 1.0 = real time, 0 = as fast as possible
    start_s: float = 0.0  # start this many seconds into the clip (snapped to a chunk boundary)


@app.post("/api/run")
async def start_run(req: RunRequest) -> dict:
    mode = req.mode or DEFAULT_MODE
    if mode == "replay" and not replay.available():
        raise HTTPException(409, "no replay cache yet; run live once first")
    if mode not in ("live", "replay"):
        raise HTTPException(400, "mode must be live or replay")
    run_id = runs.start(mode, req.speed, max(0.0, req.start_s))
    return {"run_id": run_id, "mode": mode}


@app.post("/api/stop")
async def stop_run() -> dict:
    runs.stop()
    return {"stopped": True}


@app.get("/api/state")
async def state() -> dict:
    info = config.call_info()
    return {
        "default_mode": DEFAULT_MODE,
        "running": runs.running,
        "mode": runs.mode,
        "position_s": round(runs.start_s + hub.elapsed() * runs.speed, 2) if runs.running else None,
        "integrations": config.integration_modes(),
        "replay_available": replay.available(),
        "audio": info.audio_path.exists(),
        "call": {"title": info.title, "ticker": info.ticker, "benchmark": info.primary_benchmark,
                 "clip_start": info.clip_start.isoformat(timespec="seconds")},
        "sentry": observability.enabled(),
    }


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q = hub.subscribe()

    async def stream():
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
    path = config.call_info().audio_path
    if not path.exists():
        raise HTTPException(404, "data/call.mp3 not built yet")
    return FileResponse(path, media_type="audio/mpeg")


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
