"""Gemini TTS voice alerts: speakable numbers, the deterministic sentence, gating, caching and failure paths (no network)."""

import asyncio
from types import SimpleNamespace

import pytest

from app import config, gemini, voice
from app.models import Flag

EPS_CUT_LINE = ("PepsiCo cuts core EPS growth guidance to two point five to three point five percent, "
                "from the low end of five to seven percent.")


def _flag(**kw) -> Flag:
    base = dict(id="f1", status="checked", call_ts="2026-10-08T08:20:17-04:00", t_offset=147.2, speaker="Steve Schmitt (CFO)",
                quote="driving the guide down in EPS", claim_type="guidance", metric="core_eps_growth", metric_label="Core EPS growth",
                direction="cut", new_value="+2.5% to +3.5%", prior_value="low end of +5% to +7%", materiality=0.95)
    base.update(kw)
    return Flag(**base)


class FakeTTS:
    def __init__(self, pcm_seconds: float = 3.0, fail: bool = False):
        self.calls = []
        self.pcm = b"\x01\x00" * int(24000 * pcm_seconds)
        self.fail = fail

    async def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        if self.fail:
            raise RuntimeError("503 UNAVAILABLE")
        blob = SimpleNamespace(data=self.pcm, mime_type="audio/L16;codec=pcm;rate=24000")
        part = SimpleNamespace(inline_data=blob)
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")])


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("MOCK_GEMINI", "0")
    monkeypatch.setattr(voice, "ALERT_DIR", tmp_path)

    def install(**kw) -> FakeTTS:
        tts = FakeTTS(**kw)
        monkeypatch.setattr(gemini, "client", lambda: SimpleNamespace(aio=SimpleNamespace(models=tts)))
        return tts

    return install


@pytest.mark.parametrize("value, spoken", [
    ("+2.5% to +3.5%", "two point five to three point five percent"),
    ("low end of +5% to +7%", "the low end of five to seven percent"),
    ("5-7%", "five to seven percent"),
    ("-1%", "negative one percent"),
    ("approximately +3%", "approximately three percent"),
    ("~4%", "about four percent"),
    ("+3.1% in Q3", "three point one percent in Q3"),
    ("50 bps", "fifty basis points"),
    ("$1.2 billion", "one point two billion dollars"),
    ("$8.45", "eight dollars and forty-five cents"),
    ("1,200", "one thousand two hundred"),
    ("fiscal 2026", "fiscal twenty twenty-six"),
    ("high single digits", "high single digits"),
])
def test_speakable(value, spoken):
    assert voice.speakable(value) == spoken


def test_sentence_is_built_from_the_flag_only():
    assert voice.alert_sentence(_flag(), "PepsiCo") == EPS_CUT_LINE
    raise_line = voice.alert_sentence(_flag(direction="raise", metric_label="Next-year outlook", new_value="+4%", prior_value=None), "PepsiCo")
    assert raise_line == "PepsiCo raises next-year outlook to four percent."


@pytest.mark.parametrize("change", [
    dict(direction="reaffirm"), dict(direction="new"), dict(materiality=0.8), dict(degraded=True),
    dict(status="checking"), dict(new_value=None), dict(new_value="not stated"),
])
def test_only_material_checked_cuts_and_raises_are_spoken(live, change):
    tts = live()
    assert asyncio.run(voice.alert_for(_flag(**change), config.call_info())) == (None, "")
    assert tts.calls == []


def test_mocked_gemini_returns_the_line_without_audio(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_GEMINI", "1")
    monkeypatch.setattr(voice, "ALERT_DIR", tmp_path)
    monkeypatch.setattr(gemini, "client", lambda: pytest.fail("mocked Gemini must not call the API"))
    assert asyncio.run(voice.alert_for(_flag(), config.call_info())) == (None, EPS_CUT_LINE)


def test_live_alert_writes_a_wav_once_and_then_serves_the_cache(live, tmp_path):
    tts = live()
    url, text = asyncio.run(voice.alert_for(_flag(), config.call_info()))
    assert text == EPS_CUT_LINE
    assert url.startswith("/static/alerts/core-eps-growth-cut-") and url.endswith(".wav")
    model, contents, cfg = tts.calls[0]
    assert model == config.get("GEMINI_MODEL_TTS") and cfg.response_modalities == ["AUDIO"]
    assert contents[0].parts[0].text == EPS_CUT_LINE  # spoken verbatim; style travels in speech_metadata
    wav = (tmp_path / url.rsplit("/", 1)[1]).read_bytes()
    assert voice.wav_info(wav) == (24000, 16, 1, 3.0)  # headerless PCM got a WAV header at the returned rate

    again = asyncio.run(voice.alert_for(_flag(id="f2"), config.call_info()))
    assert again == (url, text) and len(tts.calls) == 1  # same line: no second API call


def test_failures_return_nothing_and_never_raise(live):
    live(fail=True)
    assert asyncio.run(voice.alert_for(_flag(), config.call_info())) == (None, "")
    live(pcm_seconds=20)  # an over-long clip is rejected, not played
    assert asyncio.run(voice.alert_for(_flag(), config.call_info())) == (None, "")


def test_riff_audio_passes_through_unchanged():
    wav = voice.to_wav(b"\x00\x00" * 2400, "audio/L16;rate=24000")
    assert voice.to_wav(wav, "audio/wav") == wav
    assert voice.wav_info(wav)[3] == pytest.approx(0.1)
    with pytest.raises(ValueError):
        voice.wav_info(b"not a wav")


def test_self_check_live_passes_and_cleans_up(live, tmp_path):
    live()
    rows = asyncio.run(voice.self_check())
    names = [r[0] for r in rows]
    assert {"speakable numbers", "TTS returned audio", "valid WAV header", "under 10 s"} <= set(names)
    assert all(ok for _, ok, _ in rows), rows
    assert list(tmp_path.iterdir()) == []  # the test clip is deleted, the cache stays clean


def test_self_check_never_raises(live, monkeypatch):
    live(fail=True)
    rows = asyncio.run(voice.self_check())
    assert rows[0][:2] == ("speakable numbers", True) and rows[-1][1] is False
    monkeypatch.setenv("MOCK_GEMINI", "1")
    rows = asyncio.run(voice.self_check())
    assert rows[-1] == ("TTS returned audio", False, "Gemini is mocked (MOCK_GEMINI=1)")
