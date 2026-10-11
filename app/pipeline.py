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

from . import audio, briefing, config, observability, resilience
from .claims import ChunkResult, Claim, Segment, card_slot, metric_label, period_key
from .factcheck import fact_check
from .hub import EventHub
from .models import CallBrief, Flag, TranscriptSegment

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


VAGUE_STREAMING_BAR = 0.8
HEDGE_AFTER_S = 7.0  # a normal chunk returns in 3-5 s; past this, send a duplicate and take the first answer


async def hedged(make: Callable[[], Awaitable[ChunkResult]], hedge_after: float = HEDGE_AFTER_S) -> ChunkResult:
    return await resilience.hedged(make, hedge_after, label="listener")


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
    # LISTENER=live streams audio to the Gemini Live API (real time only); otherwise 12 s chunks.
    streaming = (config.get("LISTENER") or "chunks") == "live" and transcriber.name == "gemini" and speed == 1.0
    listener_name = "gemini-live" if streaming else transcriber.name

    k0 = max(0, min(int(start_s // chunk_s), int(duration // chunk_s)))  # start on a chunk boundary
    start_s = k0 * chunk_s
    hub.publish("status", {
        "state": "running", "mode": "live", "transcriber": transcriber.name, "listener": listener_name, "integrations": modes,
        "duration": duration, "chunk_s": chunk_s, "speed": speed, "audio": call_audio is not None, "start_s": start_s,
        "call": {"title": info.title, "ticker": info.ticker, "benchmark": info.primary_benchmark, "clip_start": _iso(info.clip_start)},
    })

    run_id = hub.run_id

    def publish_flag(flag: Flag) -> None:
        if hub.run_id != run_id:  # backstop: a straggler from a stopped run must not reach the next run
            log.info("dropped late publish of %s from run %s", flag.id, run_id)
            return
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
    cards: dict[str, str] = {}  # card slot -> flag id
    context: deque[str] = deque(maxlen=2)
    checks: list[asyncio.Task] = []
    n_chunks = max(1, int(-(-duration // chunk_s)))
    t0 = hub.elapsed()

    def audio_now() -> float | None:
        """Current position in the call audio, or None when not running in real time."""
        return start_s + (hub.elapsed() - t0) * speed if speed > 0 else None

    def handle(k: int, start: float, end: float, result: ChunkResult) -> None:
        """Publish a listener result: transcript lines, claim decisions, and new flags with their fact-checks."""
        for seg in result.segments:
            t = start + max(0.0, min(seg.start, end - start))
            hub.publish("transcript", TranscriptSegment(t=round(t, 1), call_ts=_iso(info.call_ts(t)), speaker=seg.speaker, text=seg.text, chunk=k).model_dump())
        if result.segments:
            context.append(" ".join(f"{s.speaker}: {s.text}" for s in result.segments))

        # One card per slot: within a result the most material claim wins the slot; later mentions attach to the card.
        best: dict[str, Claim] = {}
        for claim in result.claims:
            slot = card_slot(claim)
            if slot not in best or claim.materiality > best[slot].materiality:
                best[slot] = claim
        # Least material first, so the most material flag lands on top of the feed.
        for claim in sorted(result.claims, key=lambda c: c.materiality):
            slot = card_slot(claim)
            attached_to = cards.get(slot) or (None if best[slot] is claim else "same chunk")
            # Streaming hears one sentence at a time, so vague commentary (no value, no guidance direction)
            # needs a higher bar before it becomes a card.
            vague = streaming and not claim.new_value and claim.direction not in ("cut", "raise", "reaffirm")
            bar = max(threshold, VAGUE_STREAMING_BAR) if vague else threshold
            accepted = claim.materiality >= bar and attached_to is None
            reason = f"same metric as {attached_to}" if attached_to else ("" if accepted else ("too vague" if vague and claim.materiality >= threshold else "below threshold"))
            hub.publish("claim", {"chunk": k, "quote": claim.quote, "metric": claim.metric, "materiality": claim.materiality,
                                  "accepted": accepted, "reason": reason, "attached_to": cards.get(slot)})
            if not accepted:
                continue
            flag_id = f"f{len(cards) + 1:02d}-{claim.metric}"
            cards[slot] = flag_id
            t = start + max(0.0, min(claim.start, end - start))
            flag = Flag(
                id=flag_id, call_ts=_iso(info.call_ts(t)), t_offset=round(t, 1),
                speaker=claim.speaker, quote=claim.quote, claim_type=claim.claim_type, metric=claim.metric,
                metric_label=metric_label(claim.metric, claim.claim_type) + (f" · {claim.period}" if claim.period and period_key(claim.period).startswith("q") else ""),
                direction=claim.direction, prior_value=claim.prior_value,
                new_value=claim.new_value, materiality=claim.materiality,
            )
            now = audio_now()
            flag.latency_s = None if now is None else round(now - t, 1)
            publish_flag(flag)
            checks.append(asyncio.create_task(_safe_check(claim, flag, info, publish_flag, audio_now)))

    try:
        if streaming:
            from . import live_listener

            async for k, start, end, result in live_listener.stream(info, call_audio, start_s, audio_now, pre):
                handle(k, start, end, result)
        for k in range(k0, n_chunks) if not streaming else ():
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
            handle(k, start, end, result)

        await asyncio.gather(*checks)

        # End-of-call brief (Snowflake). Inside the run so Stop cancels it; capped and never fatal.
        checked = [t.result() for t in checks if not t.cancelled() and t.exception() is None and t.result()]
        hub.publish("brief", CallBrief(status="running").model_dump())
        try:
            brief = await asyncio.wait_for(briefing.call_brief(checked, info), timeout=45)
        except Exception as exc:  # noqa: BLE001
            observability.capture(exc)
            brief = CallBrief(status="failed", error=f"{type(exc).__name__}: {exc}"[:200])
        hub.publish("brief", brief.model_dump())
    finally:
        # A stopped run must not leave fact-checks publishing into the next run.
        extras = [t for t in (warm, pre) if t is not None]
        for task in (*checks, *extras):
            if not task.done():
                task.cancel()
        await asyncio.gather(*checks, *extras, return_exceptions=True)
    degraded = sum(1 for t in checks if not t.cancelled() and t.exception() is None and t.result() and t.result().degraded)
    summary = {"state": "done", "mode": "live", "transcriber": transcriber.name, "listener": listener_name, "integrations": modes, "flags": len(cards),
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
