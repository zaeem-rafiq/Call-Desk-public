"""Build app/mocks/mock_call.json from our own Whisper transcription of data/call.mp3.

Mock mode replays this instead of calling Gemini. Speaker turns and claims are
hand-labelled here (the claims mirror what the live Gemini extractor should find).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRT = ROOT / "data/raw/call_clip_whisper.srt"
OUT = ROOT / "app/mocks/mock_call.json"

# (start second, speaker) for each turn in the clip.
TURNS = [(0.0, "Dara Mohsenian (Morgan Stanley)"), (41.2, "Steve Schmitt (CFO)"), (211.96, "Ramon Laguarta (CEO)")]
# Whisper base.en mishears a few words; fix them so the mock reads like a good transcript.
FIXES = {"a bid Q4": "a big Q4", " 27": " 2027", "Hey, Gar,": "Hey, Dara,", "co-operating": "core operating",
         "guy down": "guide down", "ahead when": "a headwind", "and said when": "in February when", "Q423": "Q4 2023",
         "volume left": "volume led", "north of 5,": "north of 5%,", "commodity tag": "commodity type", "global soft": "global"}

CLAIMS = [
    (67.8, {"quote": "Organic revenue accelerated to 3.1%", "claim_type": "kpi", "metric": "organic_revenue_growth",
            "direction": "new", "new_value": "+3.1% in Q3", "materiality": 0.66}),
    (67.8, {"quote": "reported revenue was north of 5%", "claim_type": "kpi", "metric": "net_revenue_growth",
            "direction": "new", "new_value": "north of +5% in Q3", "materiality": 0.6}),
    (118.7, {"quote": "margin performance is below where we expected it to be", "claim_type": "margin", "metric": "operating_margin",
             "direction": "n/a", "new_value": "below plan (input costs, mix)", "materiality": 0.78}),
    (147.2, {"quote": "it's the margin piece that's driving the guide down in EPS", "claim_type": "guidance", "metric": "core_eps_growth",
             "direction": "cut", "new_value": None, "materiality": 0.97}),
    (147.2, {"quote": "sales, as you mentioned, we expect to be robust", "claim_type": "guidance", "metric": "organic_revenue_growth",
             "direction": "reaffirm", "new_value": "robust (top line holding)", "materiality": 0.72}),
    (155.5, {"quote": "using revenue management tools in the coming months to partially mitigate this margin pressure that we have in the fourth quarter",
             "claim_type": "margin", "metric": "north_america_margin", "direction": "n/a", "new_value": "pressure persists in Q4", "materiality": 0.7}),
    (202.5, {"quote": "we'll get more detailed on our 2027 expectations in February when we report the fourth quarter",
             "claim_type": "guidance", "metric": "outlook_next_year", "direction": "n/a", "new_value": "2027 detail deferred to February", "materiality": 0.7}),
    (236.9, {"quote": "That is the fastest rate of growth since Q4 2023", "claim_type": "tone", "metric": "organic_revenue_growth",
             "direction": "n/a", "new_value": "fastest since Q4 2023", "materiality": 0.5}),
]


def parse_srt(text: str) -> list[tuple[float, str]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        h, m, rest = lines[1].split(" --> ")[0].split(":")
        s, ms = rest.split(",")
        out.append((int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000, " ".join(lines[2:]).strip()))
    return out


def speaker_at(t: float) -> str:
    return [name for start, name in TURNS if start <= t][-1]


def main() -> None:
    entries = parse_srt(SRT.read_text())
    segments: list[dict] = []
    for t, text in entries:
        for a, b in FIXES.items():
            text = text.replace(a, b)
        spk = speaker_at(t)
        last = segments[-1] if segments else None
        # Merge subtitle lines into ~10-second sentences within a speaker turn.
        if last and last["speaker"] == spk and t - last["t"] < 10 and not re.search(r"[.?!]$", last["text"]):
            last["text"] += " " + text
        elif last and last["speaker"] == spk and t - last["t"] < 6:
            last["text"] += " " + text
        else:
            segments.append({"t": round(t, 2), "speaker": spk, "text": text})
    for t, claim in CLAIMS:
        seg = [s for s in segments if s["t"] <= t + 0.5][-1]
        seg.setdefault("claims", []).append({**claim, "prior_value": None})
    OUT.write_text(json.dumps({
        "_note": "MOCK transcription used only when GEMINI_API_KEY is missing: our own Whisper pass over data/call.mp3 with hand-labelled speakers and claims.",
        "duration": 288.7,
        "segments": segments,
    }, indent=1))
    print(f"wrote {OUT} with {len(segments)} segments")


if __name__ == "__main__":
    main()
