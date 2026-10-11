"""Decode the call audio once and serve fixed-length WAV chunks."""

from __future__ import annotations

import io
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16_000  # mono 16-bit PCM: plenty for speech, small inline payloads


@dataclass
class CallAudio:
    pcm: bytes
    sample_rate: int = SAMPLE_RATE

    @property
    def duration(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate

    def chunk_count(self, chunk_s: float) -> int:
        return max(1, int(-(-self.duration // chunk_s)))

    def wav_chunk(self, start_s: float, end_s: float) -> bytes:
        a = int(start_s * self.sample_rate) * 2
        b = int(min(end_s, self.duration) * self.sample_rate) * 2
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(self.pcm[a:b])
        return buf.getvalue()


def load(path: Path) -> CallAudio:
    if not path.exists():
        raise FileNotFoundError(path)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return CallAudio(pcm=out.stdout)
