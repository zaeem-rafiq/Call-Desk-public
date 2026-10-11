"""Record the README demo from replay mode: docs/media/demo.gif (sped up) and demo.mp4 (real time, with the call audio).

Usage: .venv/bin/python scripts/record_demo.py [--url http://127.0.0.1:8000] [--start 96] [--until 194]

Without --url it starts its own server in replay mode on a free port. It drives a headless Chromium over the
DevTools protocol, presses Play call, screenshots the page every --interval seconds of the call, and builds both
files with ffmpeg. No API keys are needed: replay plays the cached live run in data/replay/latest/.
Needs ffmpeg and a Chromium build: set CHROME_PATH, or run `playwright install chromium-headless-shell` once.

Defaults frame the stage replay (PepsiCo run 20261011-134427). Replay leaves out lines spoken before the start,
so the capture starts at 1:36 and the GIF opens at 2:06 with the transcript already full. The first flags land
at 2:18, the CFO says "guide down in EPS" at 2:28, and the Core EPS CUT card lands at 2:40 and stays pinned on
top to the end at 3:14.
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1440, 900


def find_chrome() -> str:
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    home = Path.home()
    patterns = [
        f"{home}/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        f"{home}/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    sys.exit("No Chromium found. Set CHROME_PATH or run `playwright install chromium-headless-shell`.")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30) -> httpx.Response:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return httpx.get(url, timeout=2).raise_for_status()
        except httpx.HTTPError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


class CDP:
    """Minimal DevTools client: send a command, wait for its reply, ignore events."""

    def __init__(self, ws):
        self.ws, self.next_id = ws, 0

    async def call(self, method: str, **params):
        self.next_id += 1
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expression: str):
        out = await self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        return out.get("result", {}).get("value")


async def capture(base: str, debug_port: int, frames_dir: Path, start: float, until: float, interval: float) -> list[float]:
    targets = wait_http(f"http://127.0.0.1:{debug_port}/json/list").json()
    page = next(t for t in targets if t.get("type") == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = CDP(ws)
        await cdp.call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT, deviceScaleFactor=1, mobile=False)
        await cdp.call("Page.navigate", url=f"{base}/?start={start:g}")
        for _ in range(100):
            if await cdp.js("document.readyState === 'complete' && !!document.querySelector('#play')"):
                break
            await asyncio.sleep(0.1)
        await cdp.js("document.fonts.ready.then(() => true)")
        await asyncio.sleep(1.0)  # let the first state and SSE reset settle
        await cdp.js("document.querySelector('#play').click()")
        stamps: list[float] = []
        t0 = time.monotonic()
        while (elapsed := time.monotonic() - t0) < until - start:
            shot = await cdp.call("Page.captureScreenshot", format="png")
            (frames_dir / f"f{len(stamps):05d}.png").write_bytes(base64.b64decode(shot["data"]))
            stamps.append(elapsed)
            await asyncio.sleep(max(0.0, t0 + len(stamps) * interval - time.monotonic()))
        return stamps


def concat_list(frames_dir: Path, stamps: list[float], speedup: float, hold: float, skip: float = 0.0) -> Path:
    """ffmpeg concat-demuxer list: each frame lasts until the next one was taken, the last one for `hold` seconds.

    Frames taken before `skip` seconds are left out.
    """
    lines = []
    for i, t in enumerate(stamps):
        if t < skip:
            continue
        dur = (stamps[i + 1] - t) / speedup if i + 1 < len(stamps) else hold
        lines += [f"file '{frames_dir / f'f{i:05d}.png'}'", f"duration {dur:.4f}"]
    lines.append(f"file '{frames_dir / f'f{len(stamps) - 1:05d}.png'}'")  # the demuxer ignores the last duration otherwise
    path = frames_dir / f"list-{speedup:g}.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="use a running Call Desk server instead of starting one")
    parser.add_argument("--start", type=float, default=96, help="clip second to start from (a 12 s chunk boundary)")
    parser.add_argument("--until", type=float, default=194, help="clip second to stop at")
    parser.add_argument("--interval", type=float, default=0.25, help="seconds of call between screenshots")
    parser.add_argument("--speedup", type=float, default=3.0, help="GIF playback speed")
    parser.add_argument("--gif-skip", type=float, default=30.0,
                        help="seconds of the recording the GIF leaves out, so it opens with the transcript full (the MP4 keeps them)")
    parser.add_argument("--gif-width", type=int, default=1200)
    parser.add_argument("--out", default=str(ROOT / "docs" / "media"))
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required (brew install ffmpeg).")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="calldesk-demo-"))
    frames = work / "frames"
    frames.mkdir()
    procs: list[subprocess.Popen] = []
    try:
        base = args.url
        if not base:
            port = free_port()
            log = open(work / "server.log", "w")
            procs.append(subprocess.Popen([sys.executable, "-m", "app.main", "--replay", "--port", str(port)],
                                          cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
            base = f"http://127.0.0.1:{port}"
        state = wait_http(f"{base}/api/state").json()
        if not state.get("replay_available"):
            sys.exit("The server has no replay cache (data/replay/latest/).")
        if state.get("running"):
            httpx.post(f"{base}/api/stop", timeout=5)
            time.sleep(1)

        debug_port = free_port()
        procs.append(subprocess.Popen([find_chrome(), "--headless", f"--remote-debugging-port={debug_port}",
                                       f"--user-data-dir={work / 'chrome'}", f"--window-size={WIDTH},{HEIGHT}",
                                       "--hide-scrollbars", "--mute-audio", "about:blank"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        print(f"recording {base} from {args.start:g}s to {args.until:g}s of the clip …")
        stamps = asyncio.run(capture(base, debug_port, frames, args.start, args.until, args.interval))
        httpx.post(f"{base}/api/stop", timeout=5)
        print(f"{len(stamps)} frames over {stamps[-1]:.1f}s")

        gif, mp4 = out / "demo.gif", out / "demo.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_list(frames, stamps, args.speedup, hold=3.0, skip=args.gif_skip)),
               "-vf", f"fps=12,scale={args.gif_width}:-1:flags=lanczos,split[a][b];"
                      "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
               "-loop", "0", str(gif))
        audio = ROOT / "data" / "call.mp3"
        if not audio.exists():
            print(f"{gif}: {gif.stat().st_size / 1e6:.1f} MB")
            print("No data/call.mp3, so no demo.mp4 (rebuild the audio with scripts/fetch_audio.py).")
            return 0
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat_list(frames, stamps, 1.0, hold=0.5)),
               "-ss", f"{args.start:g}", "-t", f"{stamps[-1] + 0.5:.2f}", "-i", str(audio),
               "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "26",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-shortest", str(mp4))
        for f in (gif, mp4):
            print(f"{f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}: {f.stat().st_size / 1e6:.1f} MB")
        return 0
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
