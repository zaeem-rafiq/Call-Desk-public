"""Rebuild data/call.mp3 from the public replay, using the offsets in data/call_meta.json.

The audio is not stored in the public repository. This downloads the public re-upload of
PepsiCo's Q3 2026 Q&A webcast with yt-dlp and trims the guidance window with ffmpeg.

Usage: .venv/bin/python scripts/fetch_audio.py [--out path]   (needs yt-dlp and ffmpeg on PATH)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = json.loads((ROOT / "data" / "call_meta.json").read_text())
RAW = ROOT / "data" / "raw"
FULL = RAW / "pep_q3_2026_full.mp3"
OUT = ROOT / "data" / "call.mp3"


def main() -> int:
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else OUT
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            print(f"{tool} is not installed (brew install {tool})")
            return 1
    RAW.mkdir(parents=True, exist_ok=True)
    if not FULL.exists():
        subprocess.run(["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(RAW / "pep_q3_2026_full.%(ext)s"), META["source_url"]], check=True)
    start, end = float(META["clip_start_s"]), float(META["clip_end_s"])
    dur = end - start
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(FULL),
        "-ac", "1", "-ar", "24000", "-b:a", "64k",
        "-af", f"afade=t=in:st=0:d=0.03,afade=t=out:st={dur - 0.06:.2f}:d=0.06", str(out),
    ], check=True)
    print(f"wrote {out} ({dur:.1f} s from {start:.1f} s of {META['source_url']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
