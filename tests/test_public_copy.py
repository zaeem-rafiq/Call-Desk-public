"""The public-copy rules: which paths leave the history, and what counts as a leaked credential."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("make_public_copy", Path(__file__).resolve().parent.parent / "scripts" / "make_public_copy.py")
mpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mpc)


@pytest.mark.parametrize("path", [
    "data/q3_transcript.txt", "data/prior/q2_2026_transcript.txt", "data/calls/dal_q3_2026/q3_transcript.txt",
    "data/call.mp3", "data/calls/dal_q3_2026/call.mp3",
    "data/prices.csv", "data/day_close.csv", "data/prior_close.csv", "data/calls/dal_q3_2026/prices.csv",
    "data/raw/pep_q3_2026_full.mp3", "data/replay/latest-prev/events.jsonl",
])
def test_third_party_paths_are_removed(path):
    assert mpc.REMOVED.search(path)


@pytest.mark.parametrize("path", [
    "data/replay/latest/transcript.txt", "data/replay/latest/events.jsonl", "data/prior/guidance_lines.csv",
    "data/prior/q2_2026_guidance.md", "data/cache/nimble_verify.json", "data/calls/pep_q3_2026/call.json",
    "app/mocks/mock_call.json",
])
def test_own_outputs_are_kept(path):
    assert not mpc.REMOVED.search(path)


@pytest.mark.parametrize("text", [
    'monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")',
    "# e.g. postgres://tsdbadmin:<pw>@xxx.tsdb.cloud.timescale.com:PORT/tsdb",
    "+GEMINI_API_KEY=",
    '+export NIMBLE_API_KEY="<TOKEN>"',
])
def test_fixtures_and_placeholders_are_not_secrets(text):
    assert not mpc.SECRETS.search(text)


@pytest.mark.parametrize("text", [
    "key = AIza" + "B" * 35,
    "+NIMBLE_API_KEY=" + "x" * 32,
    "https://" + "a1" * 16 + "@o4501234.ingest.sentry.io/123",
    "postgres://tsdbadmin:" + "s3cretpw99" + "@abc.tsdb.cloud.timescale.com:3000/tsdb",
])
def test_real_looking_credentials_are_caught(text):
    assert mpc.SECRETS.search(text)


def test_the_rules_do_not_flag_their_own_source():
    here = Path(__file__).resolve()
    for f in (here, here.parent.parent / "scripts" / "make_public_copy.py"):
        assert not mpc.SECRETS.search("\n".join("+" + line for line in f.read_text().splitlines())), f.name
