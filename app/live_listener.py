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
from collections import deque
from typing import AsyncIterator

from . import config, observability
from .audio import CallAudio
from .claims import METRICS, ChunkResult

log = logging.getLogger("calldesk")

FRAME_S = 0.1  # 100 ms of 16 kHz PCM per websocket frame
HISTORY_S = 600.0  # Gemini Live's final text can re-send a long turn minutes after its interim text was sent
# A sentence ends at a dot before a space, the end, or a capital: Gemini Live glues sentences ("rapidly.Our").
# The dot in "$1.72" or "3.1%" is no sentence end.
_END = re.compile(r"[.!?]+(?=\s|$|[A-Z])")
_ABBREV = {"mr", "mrs", "ms", "dr", "vs", "st"}

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
    nums = _digits(n)
    pool = emitted if len(n.split()) >= 4 else emitted[-24:]  # short lines ("Thank you.") recur: only recent ones repeat
    if n in pool:
        return True
    if any(n in e or (len(n) > 12 and 2 * min(len(n), len(e)) / (len(n) + len(e)) > 0.85  # cheap length check first
                      and difflib.SequenceMatcher(None, n, e).ratio() > 0.85 and nums <= _digits(e))
           for e in pool):
        return True
    words = n.split()
    if len(words) >= 8 and (_run_of_sent(_flat(n), pool, nums) or _ends_with_sent(_flat(n), emitted[-8:], nums)):
        return True
    recent = set(" ".join(emitted[-8:]).split())
    if not nums <= _digits(" ".join(emitted[-8:])):
        return False  # a new number means new information, however familiar the wording
    return len(words) >= 3 and sum(w in recent for w in words) / len(words) >= 0.85


def _digits(text: str) -> set[str]:
    """Numbers as digit runs, so interim text that glues words ("$700million") still matches the final's "700 million"."""
    return set(re.findall(r"\d+", text))


def _flat(norm: str) -> str:
    """Letters and digits only: interim text glues words ("aroughly", "fuelexpenses") that the final spaces out."""
    return norm.replace(" ", "")


def _covers(text: str, e: str, need: float = 0.85) -> int | None:
    """If `text` starts with sentence `e` (at least `need` of e's letters, in order, within a little slack for
    revisions such as "1 to 2" → "one to two"), the length of `text` that e spans; otherwise None."""
    blocks = [b for b in difflib.SequenceMatcher(None, text[:len(e) + 12], e, autojunk=False).get_matching_blocks() if b.size]
    if not blocks or blocks[0].a > 3 or sum(b.size for b in blocks) < need * len(e):
        return None
    return blocks[-1].a + blocks[-1].size


def _run_of_sent(flat: str, pool: list[str], nums: set[str]) -> bool:
    """Two or more consecutive sent sentences run together: a final re-sends a turn with periods revised to commas."""
    for i in range(len(pool)):
        if flat[:3] != _flat(pool[i])[:3]:  # cheap first-letters check
            continue
        pos, j, heard = 0, i, set()
        while j < len(pool) and (span := _covers(flat[pos:], _flat(pool[j]))) is not None:
            pos, j, heard = pos + span, j + 1, heard | _digits(pool[j])
            if len(flat) - pos < 12:
                if j - i >= 2 and nums <= heard:
                    return True
                break
    return False


def _ends_with_sent(flat: str, recent: list[str], nums: set[str]) -> bool:
    """A recent sentence with a short new start: a final restores the opening words that the interim text lost."""
    for e in recent:
        ef = _flat(e)
        if 20 <= len(ef) < len(flat) <= 2.5 * len(ef) and nums <= _digits(e):
            # read backwards, so the sentence must reach the end of the new text
            if _covers(flat[::-1], ef[::-1]) is not None:
                return True
    return False


class _Tracker:
    """Turns Gemini Live's stream of interim and final transcripts into sentences to send, each once."""

    def __init__(self) -> None:
        self._sent: deque[tuple[float, str]] = deque()  # (when, normalised) for the last HISTORY_S seconds
        self._last_done: list[str] = []
        self._last_text = ""

    def update(self, text: str, final: bool, now: float) -> list[str]:
        unchanged = text == self._last_text
        done, tail = _complete_sentences(text, settled=final or unchanged)
        if final:
            ready = done + ([tail] if tail else [])
        else:
            # Ready once more words follow it, or it is unchanged since the previous update.
            ready = done if (tail or unchanged or (done and done == self._last_done)) else done[:-1]
        self._last_done = [] if final else done
        self._last_text = "" if final else text
        return self._fresh(ready, now)

    def _fresh(self, sentences: list[str], now: float) -> list[str]:
        while self._sent and now - self._sent[0][0] > HISTORY_S:
            self._sent.popleft()
        emitted = [e for _, e in self._sent]
        out = []
        for s in sentences:
            n = _norm(s)
            if n in (emitted if len(n.split()) >= 4 else emitted[-24:]):  # most updates repeat sent sentences unchanged
                continue
            rest = _extension(s, emitted[-8:])  # checked first: a revision that adds words must send them
            if rest == "" or _seen(s if rest is None else rest, emitted):  # new words can be a sentence already sent
                continue
            out.append(rest or s)
            emitted.append(_norm(s))
            self._sent.append((now, emitted[-1]))
        return out


def _extension(sentence: str, recent: list[str]) -> str | None:
    """Interim text often ends a sentence, then revises the period to a comma and goes on ("…the Fly Delta app."
    → "…the Fly Delta app and completed the rollout…"). Return only the words after the part already sent,
    "" if a recent sentence already covers all of it, or None if it does not start with a recent sentence.
    Letters are compared without spaces, so glued interim words do not shift the cut."""
    flat = _flat(_norm(sentence))
    for e in reversed(recent):
        ef = _flat(e)
        span = _covers(flat, ef, need=0.9) if len(e.split()) >= 4 else None
        # its re-sent part must hold no new number ("item 10" is not "item 9" revised)
        if span is None or not set(re.findall(r"\d+", flat[:span])) <= set(re.findall(r"\d+", ef)):
            continue
        if len(flat) - span < 4:
            return ""
        raw = sentence.split()
        seen = 0
        for i, tok in enumerate(raw):
            seen += len(_flat(_norm(tok)))
            if seen >= span:
                if seen == span:
                    return " ".join(raw[i + 1:])
                parts = re.split(r"[.!?,;:]", tok)  # "mid-teens.capital": keep the new word, not the token
                if len(parts) > 1 and parts[-1] and len(_flat(_norm(parts[-1]))) <= seen - span:
                    return " ".join([parts[-1]] + raw[i + 1:])
                # a cut a few letters into a word is drift from glued filler words; a deeper one is no clean extension
                return " ".join(raw[i + 1:]) if seen - span <= 3 else None
    return None


def _abbreviation(head: str) -> bool:
    """True if the dot after `head` belongs to "U.S.", "e.g." or "Mr." rather than ending a sentence."""
    word = head.rsplit(None, 1)[-1].lstrip("(\"'") if head.strip() else ""
    last = word.rsplit(".", 1)[-1]
    return (len(last) == 1 and last.isalpha()) or last.lower() in _ABBREV


def _complete_sentences(text: str, settled: bool = False) -> tuple[list[str], str]:
    """Split off the finished sentences; the rest is the unfinished tail. No text is ever skipped.
    `settled`: the text is final or unchanged since the last update, so a trailing "$1." is a sentence end."""
    sentences: list[str] = []
    start = 0
    for m in _END.finditer(text):
        head = text[start:m.start()]
        if m.group() == "." and _abbreviation(head):
            continue
        if not settled and m.group() == "." and m.end() == len(text) and head[-1:].isdigit():
            break  # "profit of $1." may still become "$1.6 billion"
        if head.strip():
            sentences.append(text[start:m.end()].strip())
        start = m.end()
    return sentences, text[start:].strip()


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
            tracker = _Tracker()
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                final = sc.input_transcription.text if sc.input_transcription and sc.input_transcription.text else None
                interim_obj = getattr(sc, "interim_input_transcription", None)
                text = final or (interim_obj.text if interim_obj and interim_obj.text else None)
                if not text:
                    continue
                new = tracker.update(text, bool(final), time.monotonic())
                if new:
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
