# Call Desk: submission text

Paste each section into the matching field of the MLH or Devpost form. Every number below was measured on live services on October 11, 2026. The run IDs are in `data/replay/`.

## Name

Call Desk

## Code

https://github.com/zaeem-rafiq/Call-Desk-public. It is the full commit history, without the call audio, third-party transcripts or vendor price data; `scripts/fetch_audio.py` and `scripts/fetch_prices.py` rebuild those locally.

## Tagline (one line)

Hears earnings calls live and fact-checks every market-moving line in seconds.

## What it does

Corporate IR and FP&A teams track a dozen peer earnings calls a quarter. On the live Q&A, executives rarely repeat the numbers. On October 8, PepsiCo's CFO said "it's the margin piece that's driving the guide down in EPS" and moved on.

Call Desk listens to the call as it happens. When a market-moving line is spoken, a flag appears about 11 seconds later. A few seconds after that, the flag fills in:

- **What changed:** core EPS growth cut to +2.5% to +3.5%, from the low end of +5% to +7%. The new number is marked "not spoken", with its source.
- **Why it matters:** one sentence in a CFO's voice.
- **The evidence:** last quarter's guidance, this morning's release found on the live web, the full guidance table, and the guidance history since December.
- **The market:** PEP against the staples ETF and against its peers (KO, MDLZ, KDP), after the quote and at the close.
- **Confidence:** a transparent score that shows each component.

When the call ends, Snowflake writes an IR brief. The same engine runs a second, different call: Delta's Q3 2026, where the CFO reads the outlook aloud. There Call Desk flags full-year EPS cut from $6.50–$7.50 to $5.10–$5.60, and free cash flow cut from $3–4 billion to about $2.5 billion.

## How we built it

1. **Listen.** The call audio plays at real-time speed in 12-second chunks. Gemini 3.8 Flash transcribes each chunk, labels speakers, and extracts claims with a materiality score. Only claims scoring 0.6 or more become flags, one per metric and period. A streaming listener on the Gemini Live API is also built and turned off by default. It cuts the flag time to about 5 seconds.
2. **Check.** A Gemini fact-check agent uses function calling to call three tools in parallel:
   - Snowflake Cortex Search for prior guidance,
   - a Nimble web desk for the release and coverage,
   - Tiger Data SQL for the price reaction.

   A second Gemini call then writes a structured verdict.
3. **Score.** Confidence = 0.45 × prior + 0.35 × web + 0.20 × price. A failed tool lowers the score and never breaks the feed.
4. **Watch.** Sentry traces every agent run, model call and tool call. Each flag links to its trace.
5. **Stream.** FastAPI pushes everything to a one-page terminal-style UI over Server-Sent Events. Every run is recorded, so a replay mode can play it back with the original timing.

## Sponsor prizes

- **Gemini.** Gemini 3.8 Flash does the audio transcription, speaker labels, claim extraction and materiality scores. It also runs the fact-check agent (parallel function calling) and writes structured verdicts. Gemini Live streams transcription in the optional fast mode, and Gemini TTS speaks an alert for each guidance cut or raise.
- **Nimble.** A multi-agent web desk searches official investor-relations and SEC sources dated on the call day first, then the wider web. It extracts the release's guidance table, reads news and analyst coverage, and runs Nimble's Web Search Agent for a cited trust report. This is how Call Desk recovers numbers the executives never said aloud.
- **Tiger Data.** A `price_ticks` hypertable feeds a 1-minute continuous aggregate with real-time aggregation, stored in the columnstore (5.9× smaller). One SQL query, timed at 2 ms on Tiger, computes the move after each quote against the sector ETF and against peers.
- **Snowflake.** A warehouse holds prior-quarter guidance only, with no transcripts. Cortex Search answers "what did they guide last quarter?". A Cortex Agent re-checks each claim with Cortex Analyst over a guidance-history table. `AI_COMPLETE` writes the end-of-call IR brief inside Snowflake.
- **Sentry.** Each flag is its own `gen_ai.invoke_agent` trace. Every Gemini call is a `gen_ai.chat` span, and every tool call is a `gen_ai.execute_tool` span. Each flag shows its trace link, and browser monitoring covers the UI. Sentry caught a real timeout in our Snowflake agent during the build.

## Results (live, all five sponsors on)

- **PepsiCo golden eval: 8/8.** The EPS cut is heard on the call. All five guidance lines (core EPS, constant-currency EPS, organic revenue, net revenue, tax rate) have the right value and direction.
- **Delta golden eval: 10/10.** Full-year EPS and free-cash-flow cuts, Q4 EPS, Q4 margin, fuel and leverage.
- **Speed:** the EPS flag appears about 11 seconds after the CFO's line, and the verdict about 10 seconds later. With the Gemini Live listener, the flag appears about 5 seconds after the line.
- **Replay recordings:** 0 errors and 0 degraded flags.

## Challenges

- The PepsiCo call was Q&A only, so the guidance numbers were never spoken. We made the agent recover them from the morning release and label them "not spoken".
- A 12-second chunk boundary cut "$5.10 to | $5.60". We added a 2.5-second lead-in to each chunk, and a later mention now completes a truncated value.
- We kept the prior-quarter warehouse free of any information from after the call. An early file leaked a Q3 row, and we removed it.

## What's next

- A watchlist of peer calls running side by side.
- Alerts in Slack or Teams.
- Customer-specific "what this means for us" lines.
- Streaming as the default once its speaker labels match the chunked listener.

## Built with

Gemini API (gemini-3.8-flash, Gemini Live, Gemini TTS), Nimble, Tiger Data (TimescaleDB), Snowflake (Cortex Search, Cortex Agent, Cortex Analyst, AI_COMPLETE), Sentry, Python 3.11, FastAPI, Server-Sent Events, psycopg, pandas, vanilla JavaScript.

## AI tools used

Claude Code (Claude Opus 5.5) wrote and tested the code, researched the sponsor SDKs and prepared the demo data, under the team's direction. whisper.cpp located the demo clips offline. Inside the product: Gemini, plus the Snowflake Cortex models listed in the README's "Tools used" section.
