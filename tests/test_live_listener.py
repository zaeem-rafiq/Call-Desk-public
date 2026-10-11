"""Sentence tracking for the Gemini Live listener (no network)."""

from app.live_listener import _complete_sentences, _norm, _seen


def test_complete_sentences_keeps_the_unfinished_tail():
    done, tail = _complete_sentences("It's the margin piece driving the guide down in EPS. Uh but sales as you")
    assert done == ["It's the margin piece driving the guide down in EPS."] and tail == "Uh but sales as you"


def test_revisions_and_merged_finals_are_not_resent():
    sent = [_norm("It's the margin piece that's driving the guide down in EPS."), _norm("Uh but sales as you mentioned we expect to be robust.")]
    assert _seen("it's the margin piece that's driving the guide down in EPS", sent)  # revision
    assert _seen("So it's the margin piece that's that's driving the guide down in EPS, but sales as you mentioned we expect to be robust.", sent)
    assert not _seen("We're also identifying structural cost reduction areas.", sent)
