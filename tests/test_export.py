"""The morning note built from a recorded run."""

import json

from app import config
from app.export import morning_note


def test_morning_note_from_the_pepsico_replay():
    path = config.REPLAY_DIR / "latest" / "events.jsonl"
    history = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    note = morning_note(history)
    assert note.startswith("# PepsiCo (PEP)")
    assert "LOWERED · Core EPS growth" in note and "+2.5% to +3.5%" in note
    assert note.index("LOWERED · Core EPS growth") < note.index("## Flags") + 200  # guidance changes come first


def test_morning_note_without_flags():
    assert "No flags yet" in morning_note([])
