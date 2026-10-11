"""The live pipeline: audio chunks at real-time speed -> transcript + claims -> flags.

Chunk k is processed once real time reaches its end, so the feed behaves like a
live call. Each material claim becomes a "checking" flag immediately, and a
background fact-check fills in evidence, price reaction and confidence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from . import audio, config, observability
from .claims import ChunkResult, Claim, Segment, dedupe_key, metric_label
from .factcheck import fact_check
from .hub import EventHub
from .models import Flag, TranscriptSegment

log = logging.getLogger("calldesk")
MOCKS = Path(__file__).resolve().parent / "mocks"


class MockTranscriber:
    """Canned PepsiCo Q&A aligned to the clip, for running without a Gemini key."""

    name = "mock"

    def __init__(self) -> None:
        self.script = json.loads((MOCKS / "mock_call.json").read_text())
        self.duration = float(self.script["duration"])

    async def process(self, k: int, start: float, end: float, wav: bytes | None, context: list[str]) -> ChunkResult:
        await asyncio.sleep(0.6)  # roughly a fast model round trip
        segs, claims = [], []
        for s in self.script["segments"]:
            if start <= s["t"] < end:
                segs.append(Segment(start=s["t"] - start, speaker=s["speaker"], text=s["text"]))
                for c in s.get("claims", []):
                    claims.append(Claim(**{**c, "speaker": s["speaker"], "start": s["t"] - start}))
        return ChunkResult(segments=segs, claims=claims)


HEDGE_AFTER_S = 7.0  # a normal chunk returns in 3-5 s; past this, send a duplicate and take the first answer


async def hedged(make: Callable[[], Awaitable[ChunkResult]], hedge_after: float = HEDGE_AFTER_S) -> ChunkResult:
    """Tail-latency guard: a slow or stuck request (e.g. a Gemini 504 plus retry) gets a twin; first success wins."""
    tasks = [asyncio.create_task(make())]
    try:
        done, _ = await asyncio.wait(tasks, timeout=hedge_after)
        if not done:
            log.info("listener slow after %.0fs; sending a hedged duplicate", hedge_after)
            tasks.append(asyncio.create_task(make()))
        pending, error = set(tasks), None
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    return task.result()
                error = task.exception()
        raise error  # type: ignore[misc]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


class GeminiTranscriber:
    name = "gemini"

    def __init__(self, info: config.CallInfo) -> None:
        self.info = info

    async def process(self, k: int, start: float, end: float, wav: bytes | None, context: list[str]) -> ChunkResult:
        from . import gemini

        return await hedged(lambda: gemini.transcribe_and_extract(wav, context, k, start, end, self.info))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


async def run_live(hub: EventHub, speed: float = 1.0, start_s: float = 0.0) -> dict:
    info = config.call_info()
    chunk_s = float(config.get("CHUNK_SECONDS") or 12)
    threshold = float(config.get("MATERIALITY_THRESHOLD") or 0.6)
    modes = config.integration_modes()

    call_audio = audio.load(info.audio_path) if info.audio_path.exists() else None
    if modes["gemini"] == "live" and call_audio is not None:
        transcriber: MockTranscriber | GeminiTranscriber = GeminiTranscriber(info)
        duration = call_audio.duration
    else:
        transcriber = MockTranscriber()
        duration = call_audio.duration if call_audio else transcriber.duration

    k0 = max(0, min(int(start_s // chunk_s), int(duration // chunk_s)))  # start on a chunk boundary
    start_s = k0 * chunk_s
    hub.publish("status", {
        "state": "running", "mode": "live", "transcriber": transcriber.name, "integrations": modes,
        "duration": duration, "chunk_s": chunk_s, "speed": speed, "audio": call_audio is not None, "start_s": start_s,
        "call": {"title": info.title, "ticker": info.ticker, "benchmark": info.primary_benchmark, "clip_start": _iso(info.clip_start)},
    })

    def publish_flag(flag: Flag) -> None:
        hub.publish("flag", flag.model_dump(mode="json"))

    warm = asyncio.create_task(_warmup(modes))

    async def preroll() -> list[str]:
        """Starting mid-call: transcribe the two chunks before the start, unpublished, so the listener
        knows who is speaking (otherwise the CFO can be mistaken for an analyst and the claim discarded)."""
        spans = [(pk * chunk_s, (pk + 1) * chunk_s) for pk in range(max(0, k0 - 2), k0)]
        results = await asyncio.gather(
            *(transcriber.process(pk, a, b, call_audio.wav_chunk(a, b) if call_audio else None, [])
              for pk, (a, b) in zip(range(max(0, k0 - 2), k0), spans)),
            return_exceptions=True,
        )
        return [" ".join(f"{s.speaker}: {s.text}" for s in r.segments) for r in results if not isinstance(r, BaseException) and r.segments]

    pre = asyncio.create_task(preroll()) if k0 > 0 else None
    errors = 0
    seen: set[str] = set()
    context: deque[str] = deque(maxlen=2)
    checks: list[asyncio.Task] = []
    n_chunks = max(1, int(-(-duration // chunk_s)))
    t0 = hub.elapsed()

    def audio_now() -> float | None:
        """Current position in the call audio, or None when not running in real time."""
        return start_s + (hub.elapsed() - t0) * speed if speed > 0 else None

    try:
        for k in range(k0, n_chunks):
            start, end = k * chunk_s, min((k + 1) * chunk_s, duration)
            if speed > 0:
                await asyncio.sleep(max(0.0, (end - start_s) / speed - (hub.elapsed() - t0)))
            wav = call_audio.wav_chunk(start, end) if call_audio else None
            if pre is not None and k == k0:
                try:
                    context.extend(await asyncio.wait_for(asyncio.shield(pre), timeout=6))
                except Exception as exc:  # noqa: BLE001 - no context is better than a late first chunk
                    log.warning("preroll context unavailable: %s", exc)
            try:
                result = await transcriber.process(k, start, end, wav, list(context))
            except Exception as exc:  # noqa: BLE001 - a bad chunk must not stop the call
                observability.capture(exc)
                hub.publish("error", {"chunk": k, "message": f"chunk {k} failed: {type(exc).__name__}"})
                errors += 1
                continue

            for seg in result.segments:
                t = start + max(0.0, min(seg.start, end - start))
                hub.publish("transcript", TranscriptSegment(t=round(t, 1), call_ts=_iso(info.call_ts(t)), speaker=seg.speaker, text=seg.text, chunk=k).model_dump())
            if result.segments:
                context.append(" ".join(f"{s.speaker}: {s.text}" for s in result.segments))

            # Least material first, so the most material flag from this chunk lands on top of the feed.
            for claim in sorted(result.claims, key=lambda c: c.materiality):
                key = dedupe_key(claim)
                accepted = claim.materiality >= threshold and key not in seen
                hub.publish("claim", {"chunk": k, "quote": claim.quote, "metric": claim.metric, "materiality": claim.materiality,
                                      "accepted": accepted, "reason": "duplicate" if key in seen else ("below threshold" if not accepted else "")})
                if not accepted:
                    continue
                seen.add(key)
                t = start + max(0.0, min(claim.start, end - start))
                flag = Flag(
                    id=f"f{len(seen):02d}-{claim.metric}", call_ts=_iso(info.call_ts(t)), t_offset=round(t, 1),
                    speaker=claim.speaker, quote=claim.quote, claim_type=claim.claim_type, metric=claim.metric,
                    metric_label=metric_label(claim.metric), direction=claim.direction, prior_value=claim.prior_value,
                    new_value=claim.new_value, materiality=claim.materiality,
                )
                now = audio_now()
                flag.latency_s = None if now is None else round(now - t, 1)
                publish_flag(flag)
                checks.append(asyncio.create_task(_safe_check(claim, flag, info, publish_flag, audio_now)))

        await asyncio.gather(*checks)
    finally:
        # A stopped run must not leave fact-checks publishing into the next run.
        extras = [t for t in (warm, pre) if t is not None]
        for task in (*checks, *extras):
            if not task.done():
                task.cancel()
        await asyncio.gather(*checks, *extras, return_exceptions=True)
    degraded = sum(1 for t in checks if not t.cancelled() and t.exception() is None and t.result() and t.result().degraded)
    summary = {"state": "done", "mode": "live", "transcriber": transcriber.name, "integrations": modes, "flags": len(seen),
               "errors": errors, "degraded": degraded}
    hub.publish("status", summary)
    return summary


async def _warmup(modes: dict[str, str]) -> None:
    """Open slow connections before the first flag needs them. Failures are left to the real call."""
    if modes.get("snowflake") == "live":
        from .tools import prior_guidance

        try:
            await asyncio.to_thread(prior_guidance.warmup)
        except Exception as exc:  # noqa: BLE001
            log.warning("snowflake warmup failed: %s", exc)


async def _safe_check(claim: Claim, flag: Flag, info: config.CallInfo, publish, audio_now) -> Flag:
    def publish_with_latency(f: Flag) -> None:
        now = audio_now()
        # Only the first "checked" publish sets the headline latency; later enrichment publishes keep it.
        if f.status == "checked" and now is not None and f.checked_latency_s is None:
            f.checked_latency_s = round(now - f.t_offset, 1)
        publish(f)

    try:
        return await fact_check(claim, flag, info, publish_with_latency)
    except Exception as exc:  # noqa: BLE001 - keep the provisional flag, mark it checked with zero confidence
        observability.capture(exc)
        flag.status = "checked"
        flag.degraded = True
        flag.confidence = 0.0
        flag.confidence_rationale = f"fact-check failed: {type(exc).__name__}"
        publish_with_latency(flag)
        return flag
