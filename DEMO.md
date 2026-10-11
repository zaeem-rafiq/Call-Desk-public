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
   - `start=132` begins the clip at 2:12, which is 15 seconds before the finance chief's key sentence.
   - The header pills should all show `LIVE`.
5. Sign in to Sentry in a second tab, so the trace link opens without a login page.
6. Turn the laptop volume to about half, so you can talk over the call.
7. **Fallback:** if Wi-Fi or a key fails, click **REPLAY** in the header, then **Play call**. Replay plays the cached live run with its original timing, spends **0** agent runs, and the same `?start=132` applies.

Measured in real time with all five sponsors live (final stage run 20261011-150448): the red card appears about **12 seconds** after the finance chief's sentence, and the agent's numbers fill in about **12 seconds** after that (+24 s from the quote). The call ends with the Snowflake call summary.

## The pitch in one breath

"The stock moves the moment an executive says 'we'll earn less than we said.' Call Desk tells you how much less, with proof, in seconds."

## Plain words on stage

The judges do not work in finance. Say the plain words. Use a finance word only after its plain meaning.

| Instead of | Say |
|---|---|
| earnings call | "the call where a company tells investors how it's doing, every three months" |
| CFO | "the finance chief" |
| guidance, the guide | "the forecast" |
| guide down, cut | "lowered the forecast" |
| EPS, "Core EPS growth" (the card's title) | "profit per share", "how fast profit per share grows" |
| IR and FP&A teams | "people who follow companies for a living" |
| XLP, the staples ETF | "similar food and drink companies" |
| JETS | "other airlines" |
| the tape | "the stock price" |
| materiality | "how much it could move the stock" |
| margin | "the share of each sale kept as profit" |
| pp, percentage points | "points" |

## Script

| Time | Do | Say |
|---|---|---|
| 0:00 | Page idle | "Every three months, big companies like PepsiCo get on a call and tell investors how they're doing. One sentence can move the stock price. But on the call, executives often talk about a change without repeating the number. It's in the press release, and you have to go find it. On Thursday, PepsiCo's finance chief said 'it's the margin piece that's driving the guide down in EPS'. In plain English: 'we now expect profits to grow less than we said.' By how much? Call Desk tells you, in seconds." |
| 0:15 | Click **Play call** | "This is the real PepsiCo call from Thursday, playing in real time. Google's Gemini listens, writes it down, and picks out every sentence that could move the stock." |
| 0:30 | CFO: "it's the margin piece that's driving the guide down in EPS" | "Listen to this sentence. Notice there's no number in it." |
| ~0:42 | **Red card pops up**, showing "finding the number…" and the tool chips | "There's a red card: PepsiCo lowered its profit forecast. Now an AI agent is checking three places at the same time." |
| ~0:45 | **The spoken alert plays** ("PepsiCo cuts …") | Stop talking for 3 seconds and let the room hear it. Gemini's voice is part of the demo. |
| ~0:54 | Card fills in | "Snowflake remembers PepsiCo's forecast from three months ago: profit per share up about 5 percent this year. Nimble found PepsiCo's own press release from that morning on the web: now only 2.5 to 3.5 percent. Tiger Data watches the stock: right after the sentence, PepsiCo dipped compared with similar food and drink companies. But by the end of the day it was up 3.7 percent, 1.6 percentage points better than them. Investors shrugged off the cut." |
| 1:20 | Click **Evidence & trace** | "One click shows the proof. The forecast history from Snowflake shows this is the first cut this year. The rest of the press release shows PepsiCo raised its sales forecast to about 6 percent. Every number shows where it came from. The confidence score is 92 percent from the evidence, and two independent checks agree, which lifts it to 98. It shows its math: does the old forecast back it up, does the web back it up, and did the stock react?" |
| 1:40 | Click **Open trace in Sentry**. It opens live in your signed-in Sentry tab; show the real trace, not a screenshot | "And Sentry records every step the AI took, with timings, like a flight recorder. Anyone can check its work." |
| 1:50 | Back to the feed | "Five tools, five jobs. Gemini listens. Snowflake remembers. Nimble searches the web. Tiger Data watches the stock. Sentry keeps the record. Call Desk: you hear the change, and in seconds you see the number and its source." |

Optional closer, if time allows: open http://localhost:8000/api/export.md in a new tab. Say: "When the call ends, you get a short note you can paste into an email: every change, where it came from, and how the stock reacted."

## Numbers to have ready if asked

Each line gives the plain words first, then the finance name.

- Profit per share growth this year (FY2026 core EPS growth): +2.5% to +3.5%, from the low end of +5% to +7%.
- The same, without currency swings (core constant-currency EPS growth): +1% to +2%, from the low end of +4% to +6%.
- Sales growth without currency swings or deals (organic revenue): about +3%, from +2% to +4%.
- Total sales growth (net revenue): about +6%, from +4% to +6%.
- Tax rate (core tax rate): about 21%, from about 22%.
- Sales for July to September (Q3 net revenue) were $25.27B, up 5.6%. Organic growth was 3.1%, the fastest since late 2023 (Q4 2023).
- PepsiCo's stock (PEP) closed Oct 8 at $128.34, up 3.73%. Similar food and drink companies (XLP) were up 2.11%, and the whole market (SPY) was down 0.42%.
- **Not spoken on the call:** the forecast numbers. They appear only in the 6 AM press release. Call Desk labels them "not spoken" and shows the source.

## If a judge asks "is this built for PepsiCo only?" (30 seconds)

1. Pick **DAL · Delta Air Lines** in the header's call picker. You can also open http://localhost:8000/?call=dal_q3_2026&start=180.
2. Click **REPLAY**, then **Play call**. A replay spends 0 agent runs and needs no Wi-Fi for Gemini.
3. Say: "Same engine, different company. This is Delta Air Lines, and here the finance chief reads the forecast out loud. About 30 seconds in, Call Desk shows it: Delta now expects to earn $5.10 to $5.60 per share this year, down from a range of $6.50 to $7.50. Its forecast of cash left over after running the business fell from a range of 3 to 4 billion dollars to about 2.5 billion. The old numbers come from Snowflake, and the stock is compared with other airlines."

In the saved Delta replay, the full-year EPS card is labelled "not spoken", but the CFO did say "$5.10 to $5.60". The replay was recorded before that label was fixed, and live runs label it correctly. So don't open that card's details during the Delta beat. If asked, say: "The CFO said it out loud; that label is from an older recording."

Delta numbers to have ready:
- Profit per share for October to December (Q4 EPS): $1.15 to $1.65. Profit margin (Q4 operating margin): 7% to 9%. Fuel (Q4): about $4.25 per gallon.
- Profit per share for the full year (EPS): $5.10 to $5.60, cut from $6.50 to $7.50 in July.
- Cash left over after running the business (free cash flow): about $2.5B, cut from $3B to $4B. Debt at year end (gross leverage): 2.2×, against about 2× in the forecast.
- Delta's stock (DAL) closed Oct 9 flat (+0.04%). It was down about 3% at the 6:30 AM press release.

## If a judge asks "can it go faster?" (Gemini Live)

Say: "Yes. A streaming version puts the card on screen about 5 seconds after the sentence instead of 12. We keep it off on stage because it shows more unimportant cards."

Details for you: the stage runs the 12-second chunk listener, because its cards are cleaner. A streaming listener on the Gemini Live API (`gemini-3.5-transcribe-live`) is in the code and off by default. It sends the audio to Gemini in real time and extracts claims sentence by sentence. In live tests the PepsiCo profit card landed about 5 seconds after the line, against about 12 seconds with chunks. Start it with `LISTENER=live .venv/bin/python -m app.main`. Use it only off stage: it shows more low-value cards.

## Judge questions: short answers

- **"How accurate is it?"** "We wrote answer keys from the companies' own press releases and ran them on the live system. PepsiCo scores 8 out of 8 and Delta 10 out of 10. Any number nobody said on the call is labelled 'not spoken', with its source."
- **"How fast?"** "The red card appears about 12 seconds after the sentence, and the full answer about 12 seconds later. The streaming version gets the card up in about 5 seconds."
- **"Isn't this just ChatGPT on a transcript?"** "No. It listens live. It compares each claim with the company's last forecast in Snowflake. It finds the morning press release on the live web with Nimble. It measures the stock against similar companies in Tiger Data. And Sentry records every step. Every number shows its source and a confidence score."
- **"What if a tool fails?"** "That part of the confidence score drops to 0 and the card says so. The call keeps going. If the checking agent itself fails, the card says 'check failed · not reviewed'. It never shows made-up answers."
- **"Does it work on other companies?"** "Yes. Delta's call runs on the same engine." A new call needs a short config file, the company's last forecast, and its stock symbols.
- **"Cost?"** "One 5-minute clip makes about 40 Gemini calls: about 25 to listen and 2 for each card." We haven't measured the dollar cost, so check the Gemini usage page if a judge asks for it. Nimble agent runs are the metered part: about 2 per PepsiCo play with fresh verification, and 0 in replay.
- **"Isn't the stock move hindsight?"** "Yes, in this demo. It replays Thursday's call, so the 30-minute window and the end-of-day number use prices from after the sentence. On a live call, the window ends at the current minute and grows as the call goes on. The end-of-day number only exists after the market closes."
- **"Data rights?"** "The memory of past forecasts holds only the companies' own published forecasts. The call audio and the price data are not in the public code. Scripts rebuild them on your own computer."
