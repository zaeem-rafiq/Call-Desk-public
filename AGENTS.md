# Call Desk

Call Desk hears earnings calls live and fact-checks every market-moving line in seconds.
Built for AI Engineer NYC (MLH), October 11, 2026. Demo target: PepsiCo (PEP) Q3 2026 earnings Q&A, October 8, 2026.

## Commands

- Set up: `uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt`
- Run (live pipeline, mocks where keys are missing): `.venv/bin/python -m app.main` then open http://localhost:8000
- Run the cached replay: `.venv/bin/python -m app.main --replay`
- Which integrations are live: `.venv/bin/python scripts/check_env.py`
- Unit tests: `.venv/bin/python -m pytest -q`
- Golden eval on the audio: `.venv/bin/python tests/eval.py`

The preview pane cannot launch servers from this folder (the space in "Call Desk" breaks it). Start the server with Bash in the background and navigate the browser pane to http://localhost:8000.

## Architecture

- `app/main.py`: FastAPI, Server-Sent Events at `/api/events`, run control at `/api/run` and `/api/stop`.
- `app/pipeline.py`: plays `data/call.mp3` in `CHUNK_SECONDS` chunks at real-time speed. Gemini transcribes each chunk and extracts claims. Each material claim becomes a "checking" flag at once. A background fact-check then fills in evidence, price reaction and confidence.
- `app/factcheck.py` + `app/gemini.py`: the fact-check agent. Gemini function calling over three tools in `app/tools/`: Snowflake Cortex Search (prior guidance), Nimble (live web), Tiger Data (price reaction vs XLP).
- `app/confidence.py`: transparent weighted score (prior 0.45, web 0.35, price 0.20).
- `app/observability.py`: Sentry. One trace per flag; LLM and tool calls are gen_ai spans.
- `app/hub.py` + `app/replay.py`: every run is recorded to `data/replay/runs/<id>/events.jsonl`. The best run is promoted to `data/replay/latest/` for `--replay`.
- Every integration has a mock mode, on automatically when its key is missing (or `MOCK_<NAME>=1`).

## Data rules

- `data/q3_transcript.txt` and `data/raw/` are for evaluation only. Never feed them to the agent.
- Call transcripts (`data/q3_transcript.txt`, `data/prior/*_transcript.txt`) are Motley Fool text. They stay on this Mac and out of Git (owner decision, October 11, 2026). Early commits in this working repository still contain them.
- Publish only a copy made by `.venv/bin/python scripts/make_public_copy.py`. It removes the transcripts, the webcast audio (`call.mp3`) and the yfinance price files (`prices.csv`, `day_close.csv`, `prior_close.csv`, also under `data/calls/`) from every commit, keeps every commit and timestamp, and checks the history for credentials. Name new third-party files so its rules match them, or add a rule (tests in `tests/test_public_copy.py`). Never push that copy to this repository.
- Do not take transcripts or other data from Financial Modeling Prep. Its terms forbid showing its data in an app other people use without a separate agreement.
- The call clock maps clip offsets to real call time using `data/call_meta.json` (`estimated_clip_start_et`).
- Never print or log `.env` values. `scripts/check_env.py` reports presence only.

## Sponsor skills

`.claude/skills/` holds unmodified copies of official sponsor skills (license files kept beside each):

- `gemini-api-dev` from `google-gemini/gemini-skills` at `832c8f9`.
- `setup-timescaledb-hypertables` and `timescaledb-hyperfunctions` from `timescale/pg-aiguide` at `187be00`.
- `nimble-web-expert` from `Nimbleway/agent-skills` at `fdb077c` (MIT; the others are Apache-2.0). It is written for Nimble's CLI and MCP, which this project does not install: use it as the API reference while building Call Desk, and call Nimble through `app/tools/nimble.py`. For ordinary web lookups outside Call Desk work, do not invoke it.

To update one, copy its folder again from the official GitHub repository. Do not use `npx skills`, which is a Vercel tool. `app/gemini.py` calls `generate_content`; the Gemini skill recommends the Interactions API, so treat a migration as its own change, not part of unrelated work.

## Git and preview workflow

- Work on a task branch and commit verified units.
- Task branches may be pushed to the project's private GitHub repository, and pull requests may be opened, without asking.
- Merging into `main`, publishing, or making the repository public needs the owner's approval.
- Never send project code or documents to another AI provider unless the owner approves that destination. Gemini is approved for this product's pipeline.
