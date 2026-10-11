# Call Desk: 2-minute demo script

## Before you go on stage (5 minutes)

Nimble agent runs are a paid budget. Rehearse without fresh verification, and turn it on only for the stage run.

1. Run `.venv/bin/python scripts/check_env.py --ping`. Every line should say `LIVE` and `ping: ok`.
2. Rehearse with the plain command. It spends **0** Nimble agent runs, because verification reuses the saved results:
   ```bash
   .venv/bin/python -m app.main
   ```
3. Just before you go on stage, stop the server (Ctrl+C) and restart it with fresh verification:
   ```bash
   NIMBLE_VERIFY_FRESH=1 .venv/bin/python -m app.main
   ```
   - One play costs about **2** Nimble agent runs (core EPS and organic revenue). Every extra play costs about 2 more.
   - Set `NIMBLE_VERIFY_FRESH=1` only on this command line, never in `.env`. In `.env` it would make every eval and rehearsal spend runs.
4. Open http://localhost:8000/?start=132 in a full-screen browser window.
   - `start=132` begins the clip at 2:12, which is 15 seconds before the CFO's guidance line.
   - The header pills should all show `LIVE`.
5. Sign in to Sentry in a second tab, so the trace link opens without a login page.
6. Turn the laptop volume to about half, so you can talk over the call.
7. **Fallback:** if Wi-Fi or a key fails, click **REPLAY** in the header, then **Play call**. Replay plays the cached live run with its original timing, spends **0** agent runs, and the same `?start=132` applies.

Measured in real time with all five sponsors live (run 20261011-115458): the red flag appears about **11 seconds** after the CFO's line, and the agent's numbers fill in about **10 seconds** after that (+21 s from the quote). The call ends with the Snowflake end-of-call brief.

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

## Numbers to have ready if asked

- FY2026 core EPS growth: +2.5% to +3.5%, from the low end of +5% to +7%.
- Core constant-currency EPS growth: +1% to +2%, from the low end of +4% to +6%.
- Organic revenue: about +3%, from +2% to +4%.
- Net revenue: about +6%, from +4% to +6%.
- Core tax rate: about 21%, from about 22%.
- Q3 net revenue was $25.27B, up 5.6%. Organic growth was 3.1%, the fastest since Q4 2023.
- PEP closed Oct 8 at $128.34, up 3.73%. XLP was up 2.11% and SPY down 0.42%.
- **Not spoken on the call:** the guidance numbers. They appear only in the 6 AM release. Call Desk labels them "not spoken" and shows the source.
