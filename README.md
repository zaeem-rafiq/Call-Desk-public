# Call Desk

**Hears earnings calls live and fact-checks every market-moving line in seconds.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE) ![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi&logoColor=white) ![Built at AI Engineer NYC 2026](docs/media/badge-built-at.svg)

![Call Desk replaying PepsiCo's Q3 2026 earnings call. The live transcript fills in on the left. A red CUT card for core EPS growth appears, then fills in with the old and new guidance, the guidance table, the guidance history, the price move against XLP and a confidence of 0.92.](docs/media/demo.gif)

<sub>Replay of a recorded live run at 3× speed. The CFO says "it's the margin piece that's driving the guide down in EPS", and the card shows core EPS growth cut from the low end of +5% to +7% to +2.5% to +3.5%.</sub>

> Research demo built in one day. Not investment advice.

Corporate IR and FP&A teams track their peers' earnings calls. On a live Q&A call, executives rarely restate the numbers. They say "it's the margin piece that's driving the guide down in EPS" and move on. Call Desk catches that line as it is spoken. Within seconds it puts a flag in front of the team: what changed, why it matters, the evidence, the market reaction, and a confidence score.

**Demo:** PepsiCo (PEP) Q3 2026 earnings Q&A, October 8, 2026, using the real call audio. When the CFO says "guide down in EPS", a red flag appears. It reads: core EPS growth cut to +2.5% to +3.5%, from the low end of +5% to +7%. The new number comes from the 6 AM release (found live on the web) and the old one from last quarter's guidance (from the warehouse). The flag also shows PEP's move against XLP, the full guidance table, the guidance history since December, and a Sentry trace. Seconds later, Snowflake's Cortex Agent and a Nimble Web Search Agent re-check the claim on their own, Gemini reads the cut aloud, and when the call ends Snowflake writes a short brief for the IR team.

## Try it in one minute

Replay mode plays a recorded live run of the demo call. It needs no API keys.

```bash
git clone https://github.com/zaeem-rafiq/Call-Desk-public.git && cd Call-Desk-public
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m app.main --replay     # open http://localhost:8000/?start=132 and press "Play call"
```

`?start=132` starts 15 seconds before the CFO's guidance line. Replay plays without the call audio; to hear the call, rebuild it with `scripts/fetch_audio.py` (see [Run it](#run-it)). To run the live pipeline on your own keys, see [Run it](#run-it) too.

## Results

From the recorded live run in `data/replay/latest/` (run `20261011-115458`): all five sponsor services live, real-time speed, one call.

| Measure | Result |
|---|---|
| Golden checks passed (`tests/golden.json`) | 8 of 8 |
| CFO's "guide down in EPS" to a red flag on screen | about 11 s |
| CFO's line to the verdict with the old and new numbers | about 21 s |
| Confidence on the EPS cut | 0.92 |
| Flags raised, errors, degraded flags | 5, 0, 0 |

Latency varies with the network and model load. This is one run of one call, not a benchmark.

## How it works

```mermaid
flowchart LR
    A[call.mp3<br/>played at real-time speed] -->|12 s WAV chunks| B[Gemini 3.8 Flash<br/>listener: transcript + claims + materiality]
    B -->|transcript| UI[Browser<br/>live transcript + flag feed]
    B -->|material claims, one card per metric| F[Flag: checking]
    F --> AG[Gemini fact-check agent<br/>function calling]
    AG -->|parallel, 8 s cap each| T1[Snowflake<br/>Cortex Search + guidance history SQL]
    AG --> T2[Nimble web desk<br/>IR guidance table, web, Google, site map]
    AG --> T3[Tiger Data<br/>reaction computed in SQL on bars_1m]
    T1 & T2 & T3 --> S[Gemini verdict<br/>+ transparent confidence]
    S -->|Server-Sent Events| UI
    S --> E[After the verdict]
    E --> E1[Snowflake Cortex Agent<br/>Analyst + Search + SQL]
    E --> E2[Nimble: Web Search Agent trust report,<br/>wall-breaker, Reddit, newswire]
    E --> E3[Gemini TTS<br/>spoken alert]
    E1 & E2 & E3 -->|card fills in| UI
    UI -->|call ends| BR[Snowflake AI_COMPLETE<br/>end-of-call IR brief]
    B -. spans .-> SE[(Sentry<br/>server + browser traces)]
    AG -. spans .-> SE
```

1. **Listen.** The pipeline plays `data/call.mp3` in 12-second chunks at real-time speed. For each chunk, Gemini receives the audio and the transcript of the last two chunks. It returns speaker turns and candidate claims, each scored for materiality for a team that already has the morning release.
2. **Flag.** Each claim scoring 0.6 or more becomes a flag at once, marked "checking". A metric gets one card per call; later mentions attach to it.
3. **Fact-check.** A Gemini agent with function calling asks for three tools in parallel, each capped at 8 s:
   - Snowflake: last quarter's guidance (Cortex Search) and the metric's guidance history (SQL);
   - Nimble: the company's own guidance table from its IR site, plus web, Google and site-map specialists;
   - Tiger Data: the price reaction against XLP, computed in SQL.

   A second Gemini call writes a structured verdict: the exact numbers, what changed, why it matters, the evidence with each source's stance, and the guidance table. A CUT or RAISE badge needs numbers on both sides.
4. **Score.** Confidence is a transparent weighted sum: `0.45 × prior + 0.35 × web + 0.20 × price`. The card shows each component and its reason. A failed tool scores 0 for its component. It lowers confidence and never crashes the feed.
5. **Re-check.** After the verdict is on screen, Snowflake's Cortex Agent and Nimble's Web Search Agent each re-check the claim on their own, Nimble's wall-breaker, Reddit and newswire specialists add reaction, and Gemini speaks an alert for a material cut or raise. They fill in the card and never change the verdict.
6. **Brief.** When the call ends, the run's flags are written to Snowflake and `AI_COMPLETE` writes a headline and 3–5 bullets for the IR team inside Snowflake.

## Sponsor tools

- **Gemini API** (`gemini-3.8-flash`) does the work of five steps:
  - audio transcription and understanding, with speakers judged by what they say;
  - claim extraction with materiality scoring;
  - the fact-check agent with parallel function calling;
  - structured verdicts (slow calls are hedged: a duplicate request after 6–8 s, first answer wins);
  - spoken alerts with Gemini TTS (`gemini-3.8-flash-tts`). The sentence is built from the flag's own fields, so the voice never says a number the card doesn't show.
- **Nimble** runs a "web desk" of eight specialists for every flag, each on a different Nimble capability. They feed each other.
  - **Before the verdict**, four run in parallel within 6.5 s:
    - The *primary-source reader* uses standard-depth search limited to PepsiCo's IR site and the SEC. It returns the company's own guidance table (each line's current and previous value) in about 0.5 s. That table is the first web source in every verdict.
    - The *broad web search* runs the model's query plus a fixed recall query.
    - *Google right now* uses Fast SERP, with the standard SERP endpoint as a fallback.
    - The *IR site mapper* uses Map.
  - **After the verdict**, four more fill in the card:
    - The *verification agent* is a Web Search Agent run with `output_schema`, started from the document the primary reader found. It returns Nimble's trust report: a confidence grade, official sources, and citations for each claim. Agent runs are billed per task, so it runs only on numeric guidance lines, a run is never retried, a run past its deadline is collected later instead of repeated, and results are stored in `data/cache/nimble_verify.json` and reused (labelled "cached") unless `NIMBLE_VERIFY_FRESH=1`.
    - The *wall-breaker* uses Domain Knowledge to find which news sites run antibot systems (DataDome, Cloudflare and others), then reads the protected article with the driver Nimble recommends.
    - The *Reddit pulse* uses the pre-built `reddit_answers` Extract Template.
    - The *newswire* runs news search (`focus=news`), falling back to plain search.
- **Tiger Data** stores prices. `price_ticks` is a hypertable in the columnstore (7.6× smaller), and `bars_1m` is a 1-minute continuous aggregate with real-time aggregation. One SQL query over `bars_1m` computes PEP's move since each quote minus XLP's (the abnormal move), the day move and the close move, in about 2 ms on the server. The card shows the query and its speed.
- **Snowflake** holds the prior guidance and the call's flags. It never stores call transcripts.
  - **Cortex Search** runs over the Q2 2026 guidance documents, filtered by ticker. It answers questions such as "What was PepsiCo's core EPS guidance last quarter?"
  - **`GUIDANCE_LINES`** records every FY2026 guidance statement made before the call, from Dec 2025 to Jul 2026. The fact-check reads the metric's history from it by SQL, and the card draws that history as a timeline.
  - **The Cortex Agent `GUIDANCE_AGENT`** re-checks each claim after the verdict, inside Snowflake. It uses Cortex Analyst over the semantic view `GUIDANCE_SV`, Cortex Search, and SQL on the warehouse. The card shows its verdict (matches, changes or conflicts with prior guidance), the SQL it wrote, and the rows it read.
  - **The end-of-call brief.** The run's flags go into `CALL_FLAGS` (structured fields only), and one `AI_COMPLETE` query writes the IR brief inside Snowflake.
- **Sentry** covers errors and AI agent monitoring:
  - Each flag is its own `gen_ai.invoke_agent` trace, with the quote-to-flag and quote-to-verdict latencies as measurements.
  - Every Gemini call is a `gen_ai.chat` span; every tool call, Nimble specialist and the Cortex Agent are spans in the same trace.
  - The browser loads Sentry's official SDK (pinned with its integrity hash) and records each card's render inside that flag's server trace.
  - Each flag links to its trace.

## Run it

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/fetch_audio.py      # rebuilds data/call.mp3 from the public replay (yt-dlp + ffmpeg)
.venv/bin/python scripts/fetch_prices.py --date 2026-10-08 --tickers PEP SPY XLP --out data/prices.csv
cp .env.example .env        # fill in keys; any blank key runs that integration in mock mode
.venv/bin/python scripts/check_env.py --ping
.venv/bin/python -m app.main          # open http://localhost:8000 and press "Play call"
```

- **Seed the data stores.** Run `scripts/autoseed.py --watch` and it seeds each store as soon as its key appears. To seed by hand, run `scripts/seed_tiger.py` and `scripts/seed_snowflake.py`.
- **Golden eval.** Run `.venv/bin/python tests/eval.py`. It checks the EPS cut heard in the audio and the five FY2026 guidance lines with the correct direction. A run with Gemini mocked is labelled MOCK.
- **Unit tests.** Run `.venv/bin/python -m pytest -q`.
- **Live self-check.** Run `.venv/bin/python scripts/verify_live.py`. It sends three claims at once through every sponsor, live, and prints PASS, WARN or FAIL for each check (`--only tiger,voice,brief,sentry` runs just those). With the stored Nimble results it starts no agent runs.
- **Last-resort demo.** Run `.venv/bin/python -m app.main --replay`. It replays `data/replay/latest/` (the cached flags and transcript) with the original timing. Without the audio file `--replay` still works; a live run needs `data/call.mp3`.
- **Demo media.** Run `.venv/bin/python scripts/record_demo.py` after a UI change or a new replay cache. It records `docs/media/demo.gif` from replay mode with headless Chromium, plus a local `demo.mp4` with the call audio, which stays out of Git.

### Environment

| Variable | Unblocks |
|---|---|
| `GEMINI_API_KEY` | transcription, claim extraction, fact-check agent |
| `SENTRY_DSN`, `SENTRY_ORG` | error monitoring, AI agent traces, trace links |
| `TIGER_DATABASE_URL` | price reaction from Tiger Data |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` (+ `_WAREHOUSE`, `_DATABASE`, `_SCHEMA`) | prior-guidance search |
| `NIMBLE_API_KEY` | live web evidence |

Set `MOCK_<NAME>=1` to force a mock even when a key is present. `ENRICH=0` turns off the after-verdict re-checks. `NIMBLE_VERIFY_FRESH=1` makes the Nimble verification agent run fresh instead of reusing a stored result (each run is billed).

## Data and honesty notes

- **Audio.** `data/call.mp3` is a 4 min 49 s clip of the real Q3 2026 Q&A webcast, starting 2:50 into the call. It comes from a public re-upload, because the official webcast needs registration. The clip clock assumes the call started at 8:15:00 ET, accurate to about ±2 minutes. The audio is not in this repository: `scripts/fetch_audio.py` rebuilds it from the public re-upload (needs yt-dlp and ffmpeg).
- **What the call does and doesn't say.** PepsiCo's live call was Q&A only. The guidance numbers appear only in the 6 AM release and the posted prepared remarks. Call Desk marks any number it did not hear on the call as "not spoken", with its source.
- **Prior-quarter data.** The prior-quarter warehouse (`data/prior/`) holds only information published before the Q3 call.
- **Evaluation-only text.** `data/q3_transcript.txt` and `data/raw/` are used only for evaluation and are never given to the agent. Neither is in this repository.
- **Call transcripts are not in this repository.** They are third-party text, so they stay on the developer's machine. Without them, the prior-quarter warehouse loads only our own summary, `data/prior/q2_2026_guidance.md`, which holds every guidance number the demo checks. PepsiCo posts its own call transcripts on its [investor earnings page](https://www.pepsico.com/investors/earnings).
- **Public repository.** It is made by `scripts/make_public_copy.py` and leaves out the call audio, the vendor price files and third-party transcripts; the scripts above rebuild the audio and prices locally.
- **Cached agent results.** A Nimble verification shown as "cached" is the stored result of an earlier live run of the same check, with its time.
- **Price data.** yfinance provides 1-minute bars, not trades. Each bar is loaded as four bar-derived ticks (open, high, low, close). The continuous aggregate rebuilds exact 1-minute OHLC from them. A live feed would insert real trades into the same hypertable. The yfinance bars are not in this repository: run `scripts/fetch_prices.py` before `scripts/seed_tiger.py` (the seed reads `data/prices.csv`). Without them the two CSV price tests are skipped.

## Tools used

MLH asks every team to name the AI tools it used. This section lists every AI tool used to build Call Desk and every tool Call Desk runs on. No other AI tools were used.

### AI tools used to build Call Desk

- **Claude Code (Claude Opus 5.5)** wrote and tested the code in this repository, researched the sponsor SDKs, and prepared the demo data. Every commit carries its co-author line. Zaeem Khan decided what to build and directed the work.
- **Official sponsor skills** gave Claude Code each sponsor's own guidance. Unmodified copies are in `.claude/skills/`:
  - `gemini-api-dev` from Google (`google-gemini/gemini-skills`),
  - `setup-timescaledb-hypertables` and `timescaledb-hyperfunctions` from Tiger Data (`timescale/pg-aiguide`),
  - `nimble-web-expert` from Nimble (`Nimbleway/agent-skills`).
- **whisper.cpp (`base.en` model)** ran offline during data preparation. It located the demo clip inside the full call and produced the transcript used in mock mode (`app/mocks/mock_call.json`).

### AI services inside Call Desk

[Sponsor tools](#sponsor-tools) describes each one in detail.

- **Gemini API (`gemini-3.8-flash`)** transcribes the call, extracts claims, runs the fact-check agent, and writes the verdicts. Gemini TTS (`gemini-3.8-flash-tts`) speaks a voice alert for each guidance cut or raise.
- **Snowflake Cortex** finds last quarter's guidance with Cortex Search. Its Cortex Agent, `GUIDANCE_AGENT`, re-checks each claim with Cortex Analyst; its orchestration model is `auto`, so Snowflake chooses the model (set `SNOWFLAKE_AGENT_MODEL` to pin one). When the call ends, `AI_COMPLETE` writes the IR brief inside Snowflake with Claude Sonnet 4.6 (`claude-sonnet-4-6`), or with Llama 3.3 70B (`llama3.3-70b`) if that model errors.
- **Nimble** searches and reads the live web. Its Web Search Agent returns a trust report with citations for each claim.

### Other services and libraries

- **Tiger Data** stores the price ticks and the 1-minute bars.
- **Sentry** records errors and traces every agent, model, and tool call.
- **Open-source Python libraries:** FastAPI, Uvicorn, google-genai, snowflake-connector-python, psycopg, sentry-sdk, httpx, pandas, pydantic, and yfinance. `requirements.txt` pins their versions.

## License

Call Desk is released under the [MIT License](LICENSE). The sponsor skills in `.claude/skills/` are unmodified copies that keep their own licenses, with each license file beside its skill: `gemini-api-dev`, `setup-timescaledb-hypertables` and `timescaledb-hyperfunctions` are Apache-2.0, and `nimble-web-expert` is MIT. [Data and honesty notes](#data-and-honesty-notes) lists the third-party files this repository leaves out.
