"""Streaming listener (LISTENER=live): Gemini Live transcription + per-sentence claim extraction.

The chunked listener waits for each 12-second chunk to end before Gemini hears it, so a flag
lands ~11 s after the line. Here the call audio streams in real time to the Gemini Live API
(transcription-only model). Interim transcripts arrive about half a second behind the
speaker; each finished sentence goes to a fast text-only Gemini call that labels speakers and
extracts claims with the same schema as the chunked listener. The pipeline handles the
results exactly as it handles chunk results.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from typing import AsyncIterator

from . import config, observability
from .audio import CallAudio
from .claims import METRICS, ChunkResult

log = logging.getLogger("calldesk")

FRAME_S = 0.1  # 100 ms of 16 kHz PCM per websocket frame
_SENTENCE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)")

TEXT_LISTEN = """You are the listening layer of Call Desk, which monitors earnings calls live for corporate IR and FP&A teams.
The input is the newest sentences of a LIVE automatic transcript of {company} ({ticker}): {title}. {context}
Executives: {roster}. Analysts who may be greeted by first name: {analysts}. Label executives "Name (Role)", analysts "Name (Firm)" when known, else "Analyst".
Judge the speaker by what is said: a sentence in the company's voice ("we expect", "our guide") is an executive's.
Speakers change rarely: keep the speaker of the last CONTEXT line unless the new text clearly hands over ("I'll turn it over to Ramon",
"thanks for the question", the operator introducing someone, a greeting by name). The CFO talks guidance and margins; the CEO talks strategy.
The transcript is speech recognition and has errors ("guy down" means "guide down"; "Q423" means "Q4 2023"); write segments and quotes as the speaker meant them.

1. segments: the NEW sentences only (never the context), grouped into speaker turns, start = seconds from the first new sentence (0 if unknown).
2. claims: market-moving claims whose key words are in the NEW sentences (use context only to interpret).
   - metric: exactly one of: {metrics}.
   - direction versus prior guidance: cut | raise | reaffirm; "new" for a newly reported fact; "n/a" otherwise.
   - new_value: value as spoken with units, or null if no number is stated ("driving the guide down in EPS" is a core EPS cut with new_value null).
   - materiality 0-1, for a peer company's IR/FP&A team who already have this morning's release:
     0.9-1.0 a change to full-year guidance (stated or implied); 0.7-0.89 forward-looking information not spelled out in the release
     (margin trajectory, segment outlook, cost or pricing actions, next-year framework or its timing);
     0.4-0.59 restating the quarter's reported results, tone, color; at most 0.4 analyst questions and pleasantries.
   Return an empty claims list when nothing material is said."""


_FILLER = re.compile(r"\b(uh|um|so|and|but)\b")


def _norm(sentence: str) -> str:
    return " ".join(_FILLER.sub(" ", re.sub(r"[^a-z0-9 ]", " ", sentence.lower())).split())


def _seen(sentence: str, emitted: list[str]) -> bool:
    """True if this sentence, a revision of it, or a merge of sentences already sent (Gemini Live's final
    text often re-sends a whole turn as one sentence) is mostly made of recently sent words."""
    n = _norm(sentence)
    if not n:
        return True
    nums = {w for w in n.split() if any(ch.isdigit() for ch in w)}
    if any(n == e or n in e or (len(n) > 12 and difflib.SequenceMatcher(None, n, e).ratio() > 0.85 and nums <= set(e.split()))
           for e in emitted):
        return True
    words = n.split()
    recent = set(" ".join(emitted[-8:]).split())
    if any(any(ch.isdigit() for ch in w) and w not in recent for w in words):
        return False  # a new number means new information, however familiar the wording
    return len(words) >= 3 and sum(w in recent for w in words) / len(words) >= 0.85


def _complete_sentences(text: str) -> tuple[list[str], str]:
    sentences = [m.group(0).strip() for m in _SENTENCE.finditer(text)]
    consumed = sum(len(m.group(0)) for m in _SENTENCE.finditer(text))
    return sentences, text[consumed:].strip()


def _model() -> str:
    return config.get("GEMINI_MODEL_LIVE") or "gemini-3.5-transcribe-live"


async def extract_text(sentences: list[str], context: list[str], info: config.CallInfo) -> ChunkResult:
    """Speaker labels + claims for new transcript sentences (fast text-only Gemini call)."""
    from google.genai import types

    from . import gemini, resilience

    system = TEXT_LISTEN.format(
        company=info.company, ticker=info.ticker, title=info.title, context=info.call_context,
        roster=", ".join(info.roster) or "unknown", analysts=", ".join(info.analysts) or "unknown", metrics=", ".join(METRICS),
    )
    prompt = "CONTEXT (already processed):\n" + ("\n".join(context[-8:]) or "(start)") + "\n\nNEW SENTENCES:\n" + " ".join(sentences)

    async def call():
        resp = await gemini.client().aio.models.generate_content(
            model=config.get("GEMINI_MODEL_FAST"),
            contents=[prompt],
            config=types.GenerateContentConfig(
                system_instruction=system, response_mime_type="application/json", response_schema=ChunkResult,
                thinking_config=types.ThinkingConfig(thinking_level="low"),
                http_options=types.HttpOptions(timeout=12_000, retry_options=types.HttpRetryOptions(attempts=2, initial_delay=0.5)),
            ),
        )
        if resp.parsed is None and not resp.text:
            raise RuntimeError("empty Gemini response")
        return resp.parsed if isinstance(resp.parsed, ChunkResult) else ChunkResult.model_validate_json(resp.text)

    with observability.agent_transaction("invoke_agent call-desk-live-listener", {"calldesk.sentences": len(sentences)},
                                         agent="call-desk-live-listener"):
        result = await resilience.hedged(call, 5.0, label="live extraction")
    for c in result.claims:
        if c.metric not in METRICS:
            c.metric = "other"
    return result


async def stream(info: config.CallInfo, call_audio: CallAudio, start_s: float, audio_now,
                 context_task: asyncio.Task | None = None) -> AsyncIterator[tuple[int, float, float, ChunkResult]]:
    """Yield (batch_no, start, end, ChunkResult) as sentences finish. Times are seconds into the clip."""
    from google.genai import types

    from . import gemini

    sentences_q: asyncio.Queue = asyncio.Queue()
    results_q: asyncio.Queue = asyncio.Queue()
    context: list[str] = []
    cfg = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())

    async with gemini.client().aio.live.connect(model=_model(), config=cfg) as session:

        async def send() -> None:
            rate = call_audio.sample_rate
            step = int(rate * FRAME_S) * 2
            first = int(start_s * rate) * 2
            t0 = time.monotonic()
            for n, i in enumerate(range(first, len(call_audio.pcm), step)):
                await session.send_realtime_input(audio=types.Blob(data=call_audio.pcm[i:i + step], mime_type=f"audio/pcm;rate={rate}"))
                await asyncio.sleep(max(0.0, t0 + (n + 1) * FRAME_S - time.monotonic()))  # real time, no drift
            await session.send_realtime_input(audio_stream_end=True)

        async def receive() -> None:
            emitted: list[str] = []  # recent sentences already sent, normalised (interim text revises itself)
            last_done: list[str] = []
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                final = sc.input_transcription.text if sc.input_transcription and sc.input_transcription.text else None
                interim_obj = getattr(sc, "interim_input_transcription", None)
                text = final or (interim_obj.text if interim_obj and interim_obj.text else None)
                if not text:
                    continue
                done, tail = _complete_sentences(text)
                if final:
                    ready = done + ([tail] if tail else [])
                else:
                    # Ready once more words follow it, or it is unchanged since the previous update.
                    ready = done if (tail or (done and done == last_done)) else done[:-1]
                last_done = [] if final else done
                new = [x for x in ready if not _seen(x, emitted)]
                if new:
                    emitted.extend(_norm(x) for x in new)
                    del emitted[:-24]
                    await sentences_q.put((audio_now() or 0.0, new))

        async def extract() -> None:
            batch_no = 0
            pending_context = context_task
            while True:
                first = await sentences_q.get()
                if first is None:
                    break
                items = [first]
                while not sentences_q.empty():  # coalesce whatever finished while the last call ran
                    nxt = sentences_q.get_nowait()
                    if nxt is None:
                        sentences_q.put_nowait(None)
                        break
                    items.append(nxt)
                if pending_context is not None:  # mid-call start: the chunked preroll says who was speaking
                    try:
                        context[:0] = await asyncio.wait_for(asyncio.shield(pending_context), timeout=4)
                        pending_context = None
                    except asyncio.TimeoutError:
                        log.info("live preroll context not ready yet")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("live preroll context failed: %r", exc)
                        pending_context = None
                sentences = [s for _, batch in items for s in batch]
                end = items[-1][0]
                start = max(start_s, items[0][0] - 1.5 * len(items[0][1]))  # rough: ~1.5 s per sentence before it finished
                try:
                    result = await extract_text(sentences, context, info)
                except Exception as exc:  # noqa: BLE001 - a failed batch must not stop the call
                    observability.capture(exc)
                    log.warning("live extraction failed: %s", exc)
                    continue
                context.extend(f"{s.speaker}: {s.text}" for s in result.segments)
                await results_q.put((batch_no, start, max(end, start + 0.1), result))
                batch_no += 1
            await results_q.put(None)

        sender = asyncio.create_task(send())
        receiver = asyncio.create_task(receive())
        extractor = asyncio.create_task(extract())
        try:
            async def finish() -> None:
                await sender
                await asyncio.sleep(4.0)  # let the last transcription arrive
                receiver.cancel()
                await sentences_q.put(None)

            closer = asyncio.create_task(finish())
            while True:
                item = await results_q.get()
                if item is None:
                    break
                yield item
            await closer
        finally:
            for task in (sender, receiver, extractor):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender, receiver, extractor, return_exceptions=True)
