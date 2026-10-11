# Call Desk: 2-minute demo script

## Before you go on stage (5 minutes)

Nimble agent runs are a paid budget. Rehearse without fresh verification, and turn it on only for the stage run.

1. Run `.venv/bin/python scripts/check_env.py --ping`. Every line should say `LIVE` and `ping: ok`.
2. Rehearse with this command. It spends **0** Nimble agent runs, because verification reuses the saved results. `REPLAY_FREEZE=1` keeps rehearsals from replacing the recorded replay backup:
   ```bash
   REPLAY_FREEZE=1 .venv/bin/python -m app.main
   ```
3. Just before you go on stage, stop the server (Ctrl+C) and restart it with fresh verification:
   ```bash
   REPLAY_FREEZE=1 NIMBLE_VERIFY_FRESH=1 .venv/bin/python -m app.main
   ```
   - One play costs about **2** Nimble agent runs (core EPS and organic revenue). Every extra play costs about 2 more.
   - Set `NIMBLE_VERIFY_FRESH=1` only on this command line, never in `.env`. In `.env` it would make every eval and rehearsal spend runs.
4. Open http://localhost:8000/?start=132 in a full-screen browser window.
   - `start=132` begins the clip at 2:12, which is 15 seconds before the CFO's guidance line.
   - The header pills should all show `LIVE`.
5. Sign in to Sentry in a second tab, so the trace link opens without a login page.
6. Turn the laptop volume to about half, so you can talk over the call.
7. **Fallback:** if Wi-Fi or a key fails, click **REPLAY** in the header, then **Play call**. Replay plays the cached live run with its original timing, spends **0** agent runs, and the same `?start=132` applies.

Measured in real time with all five sponsors live (final stage run 20261011-134427): the red flag appears about **12 seconds** after the CFO's line, and the agent's numbers fill in about **12 seconds** after that (+24 s from the quote). The call ends with the Snowflake end-of-call brief.

## Script

| Time | Do | Say |
|---|---|---|
| 0:00 | Page idle | "Every quarter, IR and FP&A teams sit through a dozen peer earnings calls. On the live Q&A, executives don't repeat the numbers. On Thursday, PepsiCo's CFO said 'it's the margin piece that's driving the guide down in EPS' and moved on. Call Desk catches that line and tells you exactly what changed, within seconds." |
| 0:15 | Click **Play call** | "This is the real PepsiCo Q3 call from October 8, in real time. Gemini 3.8 Flash hears it in 12-second chunks. It transcribes, pulls out every claim, and scores how much each one could move the stock." |
| 0:30 | CFO: "it's the margin piece that's driving the guide down in EPS" | "Listen to that line. The CFO never says a number." |
| ~0:42 | **Red CUT flag pops**, showing "finding the number…" and the tool chips | "Seconds later there's a red flag: core EPS guidance cut. Now the agent is calling three tools in parallel." |
| ~0:52 | Flag fills in | "Snowflake Cortex Search has last quarter's guidance: the low end of 5 to 7 percent. Nimble found this morning's release on the live web: 2.5 to 3.5 percent. That's the first formal cut this year, and the table shows revenue guidance going up to about 6 percent. Tiger Data has the tape: PEP against the staples ETF dipped right after the remark. By the close, PEP was up 3.7 percent, 1.6 points better than staples. The market looked through the cut." |
| 1:30 | Click **Evidence & trace** | "Every number has a source and a stance. Confidence is a transparent score: prior guidance, web corroboration, and price confirmation, each shown." |
| 1:40 | Click **Open trace in Sentry** | "And every flag is a Sentry trace: the agent run, each Gemini call, and each tool call, with timings." |
| 1:50 | Back to the feed | "Gemini, Nimble, Tiger Data, Snowflake and Sentry. Call Desk: every market-moving line, fact-checked in seconds." |

Optional closer, if time allows: open http://localhost:8000/api/export.md in a new tab. Say: "When the call ends, the IR team gets a morning note it can paste and send: every change, its source, and the market reaction."

## Numbers to have ready if asked

- FY2026 core EPS growth: +2.5% to +3.5%, from the low end of +5% to +7%.
- Core constant-currency EPS growth: +1% to +2%, from the low end of +4% to +6%.
- Organic revenue: about +3%, from +2% to +4%.
- Net revenue: about +6%, from +4% to +6%.
- Core tax rate: about 21%, from about 22%.
- Q3 net revenue was $25.27B, up 5.6%. Organic growth was 3.1%, the fastest since Q4 2023.
- PEP closed Oct 8 at $128.34, up 3.73%. XLP was up 2.11% and SPY down 0.42%.
- **Not spoken on the call:** the guidance numbers. They appear only in the 6 AM release. Call Desk labels them "not spoken" and shows the source.

## If a judge asks "is this built for PepsiCo only?" (30 seconds)

1. Pick **DAL · Delta Air Lines** in the header's call picker. You can also open http://localhost:8000/?call=dal_q3_2026&start=180.
2. Click **REPLAY**, then **Play call**. A replay spends 0 agent runs and needs no Wi-Fi for Gemini.
3. Say: "Same engine, different company and a different call format. Delta's CFO reads the outlook aloud. About 30 seconds in, Call Desk flags full-year EPS cut from $6.50–$7.50 to $5.10–$5.60, and free cash flow cut from $3–4 billion to about $2.5 billion. The prior numbers come from Snowflake, and the tape is DAL against the airline ETF, JETS."

Delta numbers to have ready:
- Q4 EPS $1.15 to $1.65. Q4 operating margin 7% to 9%. Q4 fuel about $4.25 per gallon.
- Full-year EPS $5.10 to $5.60, cut from $6.50 to $7.50 in July.
- Free cash flow about $2.5B, cut from $3B to $4B. Year-end gross leverage 2.2×, against about 2× guided.
- DAL closed Oct 9 flat (+0.04%). It was down about 3% at the 6:30 AM release.

## If a judge asks "can it go faster?" (Gemini Live)

The stage runs the 12-second chunk listener, because its cards are cleaner. A streaming listener on the Gemini Live API (`gemini-3.5-transcribe-live`) is in the code and off by default. It sends the audio to Gemini in real time and extracts claims sentence by sentence. In live tests the PepsiCo EPS flag landed about 5 seconds after the line, against about 12 seconds with chunks. Start it with `LISTENER=live .venv/bin/python -m app.main`. Use it only off stage: it shows more low-value cards.

## Judge questions: short answers

- **"How accurate is it?"** We wrote golden tests from the companies' own releases and run them on the live stack. PepsiCo passes 8/8 and Delta 10/10. Any number not said on the call is labelled "not spoken", with its source.
- **"How fast?"** The flag appears about 12 seconds after the line, and the verdict about 12 seconds later. The Gemini Live listener gets the flag to about 5 seconds.
- **"Isn't this just ChatGPT on a transcript?"** No. It listens live, compares each claim with last quarter's guidance in Snowflake, checks the morning release on the live web with Nimble, measures the tape against the sector ETF and peers in Tiger Data, and traces every step in Sentry. Every number shows its source and a confidence score.
- **"What if a tool fails?"** That part of the confidence score drops to 0 and the card says so. The feed keeps going. If the agent itself fails, the card is marked "agent failed · unreviewed evidence", and it never shows canned answers.
- **"Does it work on other companies?"** Yes. Delta's call runs on the same engine (see the Delta section above). A new call needs a short config file, its prior guidance, and its price tickers.
- **"Cost?"** One 5-minute clip makes roughly 40 Gemini calls: about 25 for listening and 2 per flag. We haven't measured the dollar cost, so check the Gemini usage page if a judge asks for it. Nimble agent runs are the metered part: about 2 per PepsiCo play with fresh verification, and 0 in replay.
- **"Data rights?"** The prior-quarter warehouse holds only the companies' own published guidance. The call audio and vendor price data are not in the public repository; scripts rebuild them locally.
