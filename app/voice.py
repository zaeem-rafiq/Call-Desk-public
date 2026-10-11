"""Gemini TTS voice alerts for guidance cuts and raises.

alert_for(flag, info) -> (audio_url or None, spoken text). Called by factcheck._enrich after the verdict.
The sentence is built from the flag's own fields (no LLM text, so no invented numbers), spoken once by
Gemini TTS and cached in static/alerts/ by text hash, so replays and repeat runs cost nothing.
self_check() -> list of (name, ok, detail) rows for scripts/verify_live.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
import wave
from pathlib import Path

from . import config, observability
from .models import Flag

log = logging.getLogger("calldesk")

ALERT_DIR = config.STATIC / "alerts"
URL_PREFIX = "/static/alerts/"
MIN_MATERIALITY = 0.85
SYNTH_TIMEOUT_S = 15
MAX_AUDIO_S = 15.0
MAX_WORDS = 32  # past this, drop the "from <prior>" clause to keep the alert short
VOICE = "Kore"  # prebuilt voice: firm, clear
STYLE = "Calm, crisp financial newsroom alert. Brisk pace."

# ---------------------------------------------------------------- speakable numbers
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand"))
_SCALE_WORDS = {"trillion": "trillion", "tn": "trillion", "billion": "billion", "bn": "billion", "b": "billion",
                "million": "million", "mm": "million", "m": "million", "thousand": "thousand", "k": "thousand"}

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"  # "1,200", "2.5"; never eats a list comma
_SIGN = r"(?<![\w.])([+-]?)"  # a sign only when it is not glued to a word or number ("Q3-2026", "5-7")


def _int_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {_int_words(rest)}" if rest else "")
    for size, name in _SCALES:
        if n >= size:
            head, rest = divmod(n, size)
            return f"{_int_words(head)} {name}" + (f" {_int_words(rest)}" if rest else "")
    return str(n)


def number_words(token: str) -> str:
    """'2.5' -> 'two point five', '1,200' -> 'one thousand two hundred'."""
    whole, _, frac = token.replace(",", "").partition(".")
    words = _int_words(int(whole or "0"))
    if frac:
        words += " point " + " ".join(_ONES[int(d)] for d in frac)
    return words


def _year_words(n: int) -> str:
    if 2000 <= n < 2010:
        return _int_words(n)
    century, rest = divmod(n, 100)
    return f"{_int_words(century)} " + (_int_words(rest) if rest >= 10 else f"oh {_ONES[rest]}" if rest else "hundred")


def _signed(sign: str, token: str) -> str:
    return ("negative " if sign == "-" else "") + number_words(token)


def _dollars(sign: str, token: str, scale: str | None) -> str:
    if scale:
        return f"{_signed(sign, token)} {_SCALE_WORDS[scale.lower()]} dollars"
    whole, _, cents = token.replace(",", "").partition(".")
    if len(cents) == 2:
        out = f"{_int_words(int(whole or '0'))} dollars"
        if int(cents):
            out += f" and {_int_words(int(cents))} cents"
        return ("negative " if sign == "-" else "") + out
    return f"{_signed(sign, token)} dollars"


def speakable(value: str) -> str:
    """Turn a spoken-guidance value into words a TTS voice reads reliably.

    "+2.5% to +3.5%" -> "two point five to three point five percent"
    "low end of +5% to +7%" -> "the low end of five to seven percent"
    """
    s = " ".join(value.replace("−", "-").replace("–", "-").replace("—", "-").split())
    s = re.sub(r"~\s*", "about ", s)
    # ranges that share one unit: "+2.5% to +3.5%", "5-7%", "50 to 75 bps"
    s = re.sub(rf"{_SIGN}({_NUM})\s*%?\s*(?:to|-)\s*([+-]?)({_NUM})\s*%",
               lambda m: f"{_signed(m[1], m[2])} to {_signed(m[3], m[4])} percent", s)
    s = re.sub(rf"{_SIGN}({_NUM})\s*(?:to|-)\s*([+-]?)({_NUM})\s*(?:bps|bp|basis points)\b",
               lambda m: f"{_signed(m[1], m[2])} to {_signed(m[3], m[4])} basis points", s)
    s = re.sub(rf"{_SIGN}({_NUM})\s*%", lambda m: f"{_signed(m[1], m[2])} percent", s)
    s = re.sub(rf"{_SIGN}({_NUM})\s*(?:bps|bp|basis points)\b", lambda m: f"{_signed(m[1], m[2])} basis points", s)
    s = re.sub(rf"(?<![\w.])([+-]?)\$\s*({_NUM})(?:\s*(trillion|billion|million|thousand|tn|bn|mm|b|m|k)\b)?",
               lambda m: _dollars(m[1], m[2], m[3]), s, flags=re.IGNORECASE)
    s = re.sub(rf"(?<![\w.])({_NUM})\s*-\s*({_NUM})(?!\w|\.\d)", lambda m: f"{number_words(m[1])} to {number_words(m[2])}", s)
    s = re.sub(r"(?<![\w.,$])(19\d\d|20\d\d)(?!\w|[.,]\d)", lambda m: _year_words(int(m[1])), s)
    s = re.sub(rf"{_SIGN}({_NUM})(?!\w|\.\d)", lambda m: _signed(m[1], m[2]), s)
    s = re.sub(r"^(low|high|lower|upper) end\b|^midpoint\b|^bottom\b|^top\b", lambda m: f"the {m[0]}", s, flags=re.IGNORECASE)
    return " ".join(s.split())


# ---------------------------------------------------------------- the alert sentence
def _has_value(value: str | None) -> bool:
    return bool(value) and bool(re.search(r"\d|digit", value, re.IGNORECASE))


def eligible(flag: Flag) -> bool:
    """Speak only checked, high-materiality guidance cuts and raises that carry a number."""
    return (flag.status == "checked" and flag.direction in ("cut", "raise") and flag.materiality >= MIN_MATERIALITY
            and not flag.degraded and _has_value(flag.new_value))


def _spoken_label(label: str) -> str:
    words = label.split()
    if words and not words[0].isupper():
        words[0] = words[0].lower()
    out = " ".join(words)
    return out if re.search(r"guidance|outlook|forecast", out, re.IGNORECASE) else f"{out} guidance"


def alert_sentence(flag: Flag, company: str) -> str:
    """'PepsiCo cuts core EPS growth guidance to two point five to three point five percent, from ...'"""
    verb = "cuts" if flag.direction == "cut" else "raises"
    head = f"{company} {verb} {_spoken_label(flag.metric_label)} to {speakable(flag.new_value or '')}"
    if _has_value(flag.prior_value):
        full = f"{head}, from {speakable(flag.prior_value or '')}"
        if len(full.split()) <= MAX_WORDS:
            return full + "."
    return head + "."


# ---------------------------------------------------------------- audio
def to_wav(data: bytes, mime_type: str | None) -> bytes:
    """3.8 TTS returns a complete WAV; older or streamed output is headerless 16-bit mono PCM."""
    if data[:4] == b"RIFF":
        return data
    m = re.search(r"rate=(\d+)", mime_type or "")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(m[1]) if m else 24000)
        w.writeframes(data)
    return buf.getvalue()


def wav_info(data: bytes) -> tuple[int, int, int, float]:
    """(sample rate, bits, channels, seconds). Raises on anything that is not a PCM WAV."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, width, channels, frames = w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes()
    block = width * channels
    if rate <= 0 or block <= 0:
        raise ValueError("bad WAV format")
    frames = min(frames, max(0, len(data) - 44) // block)  # streamed headers can carry a placeholder size
    return rate, width * 8, channels, frames / rate


async def _synthesize(text: str) -> bytes:
    from google.genai import types

    from . import gemini

    resp = await gemini.client().aio.models.generate_content(
        model=config.get("GEMINI_MODEL_TTS"),
        contents=[types.Content(role="user", parts=[
            types.Part(text=text, speech_metadata=types.SpeechMetadata(style=STYLE))])],
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        ),
    )
    cands = resp.candidates or []
    parts = (cands[0].content.parts if cands and cands[0].content else None) or []
    blob = next((p.inline_data for p in parts if p.inline_data and p.inline_data.data), None)
    if blob is None:
        raise RuntimeError(f"no audio in TTS response ({cands[0].finish_reason if cands else 'no candidates'})")
    return to_wav(blob.data, blob.mime_type)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _cached(text: str) -> Path | None:
    return next(iter(sorted(ALERT_DIR.glob(f"*-{_digest(text)}.wav"))), None)


def _save(wav: bytes, slug: str, text: str) -> Path:
    ALERT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:40] or "alert"
    path = ALERT_DIR / f"{slug}-{_digest(text)}.wav"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(wav)
    os.replace(tmp, path)  # atomic: a reader never sees half a file
    return path


async def alert_for(flag: Flag, info: config.CallInfo) -> tuple[str | None, str]:
    try:
        if not eligible(flag):
            return None, ""
        text = alert_sentence(flag, info.company)
        if config.is_mock("gemini"):
            return None, text  # the card still shows the line
        path = _cached(text)
        if path is None:
            wav = await asyncio.wait_for(_synthesize(text), timeout=SYNTH_TIMEOUT_S)
            seconds = wav_info(wav)[3]
            if not 0 < seconds <= MAX_AUDIO_S:
                raise ValueError(f"TTS audio is {seconds:.1f}s")
            path = _save(wav, f"{flag.metric}-{flag.direction}", text)
            log.info("voice alert %s (%.1fs): %s", path.name, seconds, text)
        return URL_PREFIX + path.name, text
    except Exception as exc:  # noqa: BLE001 - a missing alert is fine; the card stands
        observability.capture(exc)
        return None, ""


# ---------------------------------------------------------------- live self-check
SPEAKABLE_CASES = {
    "+2.5% to +3.5%": "two point five to three point five percent",
    "low end of +5% to +7%": "the low end of five to seven percent",
}
TEST_SENTENCE = "Call Desk voice check. PepsiCo cuts core EPS growth guidance to two point five to three point five percent."


async def self_check() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    try:
        got = [speakable(k) for k in SPEAKABLE_CASES]
        rows.append(("speakable numbers", got == list(SPEAKABLE_CASES.values()), " | ".join(got)))
        reason = config.mock_reason("gemini")
        if reason:
            rows.append(("TTS returned audio", False, f"Gemini is mocked ({reason})"))
            return rows
        model = config.get("GEMINI_MODEL_TTS")
        t0 = time.monotonic()
        wav = await asyncio.wait_for(_synthesize(TEST_SENTENCE), timeout=60)
        took = time.monotonic() - t0
        rows.append(("TTS returned audio", len(wav) > 10_000, f"{len(wav) / 1024:.0f} KB in {took:.1f}s · {model} · {VOICE}"))
        try:
            rate, bits, channels, seconds = wav_info(wav)
        except Exception as exc:  # noqa: BLE001
            rows.append(("valid WAV header", False, f"{type(exc).__name__}: {exc}"[:120]))
            return rows
        rows.append(("valid WAV header", True, f"{rate} Hz · {bits}-bit · {channels} ch"))
        rows.append(("under 10 s", 0 < seconds < 10, f"{seconds:.1f}s of audio"))
        path = _save(wav, "selfcheck", f"{TEST_SENTENCE}{time.time()}")  # never collides with a cached alert
        try:
            rows.append(("saved under /static/alerts/", path.stat().st_size == len(wav), URL_PREFIX + path.name))
        finally:
            path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - self_check never raises
        rows.append(("TTS self-check", False, f"{type(exc).__name__}: {exc}"[:160]))
    return rows
