# Call Desk: submission text

Paste each section into the matching field of the MLH submission form. Every number below was measured on live services on October 11, 2026. The run IDs are in `data/replay/`.

The judges do not work in finance, so this text uses everyday words. A finance term appears only after its plain meaning.

## Name

Call Desk

## Code

https://github.com/zaeem-rafiq/Call-Desk-public. It is the full commit history, without the call audio, third-party transcripts or vendor price data. `scripts/fetch_audio.py` and `scripts/fetch_prices.py` rebuild those locally.

## One-line pitch (at most 140 characters)

The stock moves the moment an executive says “we'll earn less than we said.” Call Desk tells you how much less, with proof, in seconds.

## Description

Paste the five sections below, from "What it does" to "What's next", into the description field.

## What it does

Every three months, big companies like PepsiCo hold an earnings call. Their executives tell investors how the business is doing and what they expect next. One sentence can move the stock price.

On the call, executives often talk about a change without repeating the number. The number is in the company's press release, so listeners must stop and look it up. On October 8, 2026, PepsiCo's finance chief said "it's the margin piece that's driving the guide down in EPS". In plain words: "We now expect profits to grow less than we said before." The exact number was in that morning's press release, not in the sentence.

Call Desk listens to the call live. About 12 seconds after that sentence, a red card pops up: PepsiCo lowered its profit forecast. About 12 seconds later, the card fills in:

- **Before and now:** profit per share was expected to grow about 5% this year. Now it is 2.5% to 3.5%. The new number is labelled "not spoken", because it comes from PepsiCo's press release that morning, not from the call.
- **Proof:** the press release, found live on the web, and the forecast PepsiCo gave three months earlier.
- **The stock:** how PepsiCo's stock moved, next to similar food and drink companies.
- **How sure:** a confidence score of 92%, with its reasons shown.

Think of a sports announcer who shouts "that changes the score!" but never says the score. Call Desk is the friend beside you who checks the scoreboard and says: "It was 5, now it's 3. Here's a photo of the scoreboard."

It is not built for one company. On Delta Air Lines' call, the finance chief reads the forecast out loud. Call Desk shows that Delta now expects to earn $5.10 to $5.60 per share this year, down from a range of $6.50 to $7.50. It also shows that Delta lowered its forecast of cash left over after running the business (free cash flow), from a range of $3–4 billion to about $2.5 billion.

When the call ends, Call Desk writes a short summary that you can paste into an email.

## How we built it

Five tools work together. Each one has one job.

1. **Listen (Gemini).** The call plays at real-time speed in 12-second pieces. Google's Gemini 3.8 Flash writes down each piece, labels who is speaking, and picks out claims. It gives each claim an importance score from 0 to 1: how much it could move the stock. Claims scoring 0.6 or more become cards, one card per topic. A faster streaming listener on the Gemini Live API is also built. It cuts the time to a card to about 5 seconds, and it is off by default.
2. **Check (Gemini and three tools).** An AI agent built on Gemini calls three tools at the same time:
   - **Snowflake** remembers what the company forecast last time (Cortex Search).
   - **Nimble** searches the live web for the company's press release and news coverage.
   - **Tiger Data** measures how the stock moved, with one SQL query.

   A second Gemini call then writes the card's answer.
3. **Score.** Confidence = 0.45 × old-forecast check + 0.35 × web check + 0.20 × stock check. A failed tool lowers the score. It never stops the call. After the answer, Snowflake's agent and Nimble's agent check again. Each one that agrees adds 3 points, each one that disagrees takes away 10, and the score never goes above 98%.
4. **Keep a record (Sentry).** Sentry records every agent run, AI model call and tool call. Each card links to its record.
5. **Show.** A Python web server (FastAPI) streams everything to a one-page app. Every run is recorded, so a replay mode can play it back with the original timing.

## Results (live, all five tools on)

- **PepsiCo answer key: 8 of 8.** We wrote the answer key from the company's own press release.
  - Three checks cover what was said on the call: the lowered profit forecast, pressure on profit margins, and next year's forecast being put off until February.
  - Five checks cover the numbers. Each forecast line must have the right value and the right direction (up, down or unchanged). The lines are two measures of profit per share, two measures of sales, and the tax rate.
- **Delta answer key: 10 of 10.** It catches the lowered full-year forecasts for profit per share and for cash left over after running the business (free cash flow). It also gets the forecasts for the last three months of the year (profit per share, profit margin and fuel cost) and Delta's year-end debt level.
- **Speed:** the red card appears about 12 seconds after the finance chief's sentence, and the full answer about 12 seconds after that (the run we show on stage, 20261011-134427). With the Gemini Live listener, the card appears about 5 seconds after the sentence.
- **Replay recordings:** no pipeline errors, and no cards where the checking agent failed. Some optional Nimble specialists (for example the news search) were unavailable at times. The card shows them as unavailable, and the main checks still ran.

## Challenges

- PepsiCo's call was questions and answers only, so nobody read the forecast numbers out loud. We taught the agent to find them in that morning's press release and to label them "not spoken".
- Cutting the audio into 12-second pieces once split "$5.10 to | $5.60" in half. Each piece now starts 2.5 seconds early, and a later mention completes a number that was cut off.
- The memory of earlier forecasts must not know anything from after the call, or the check would be cheating. An early file let one later number slip in, and we removed it.

## What's next

- Watch several companies' calls side by side.
- Send alerts to Slack or Teams.
- Explain what each change means for the person reading it.
- Make the faster streaming listener the default once it labels speakers as well as the 12-second listener does.

## Prize category answers (at most 1000 characters each)

Paste each answer into its category's field. Each one is under 1000 characters.

### Best Overall

Every three months, big companies hold a call to tell investors how they are doing. One sentence can move the stock. But on the call, executives often mention a change without repeating the number. Call Desk listens live. On PepsiCo's real call on October 8, 2026, the finance chief said "it's the margin piece that's driving the guide down in EPS", meaning profits will grow less than forecast. About 12 seconds later, a red card appeared. About 12 seconds after that, it showed the new forecast (profit per share up 2.5% to 3.5%, down from about 5%), the press release it came from, the old forecast, how the stock moved next to similar companies, and a 92% confidence score with its math. The same engine works on Delta Air Lines' call. It uses all five sponsors, each with one clear job. It scores 8 of 8 and 10 of 10 on answer keys written from the companies' own press releases.

### Gemini

Gemini is the ears and the brain of Call Desk. Gemini 3.8 Flash listens to the live call audio in 12-second pieces. It writes down what is said, labels who is speaking, picks out claims, and gives each claim an importance score. When a claim matters, a Gemini agent calls three tools at the same time (parallel function calling): Snowflake for the old forecast, Nimble for the web, and Tiger Data for the stock price. A second Gemini call turns the evidence into a structured answer card with the old and new numbers. Gemini TTS reads an alert out loud for each forecast that goes up or down, using only numbers shown on the card. A faster listener on the Gemini Live API streams the audio and puts a card on screen about 5 seconds after the sentence. On PepsiCo's real call, a card appears about 12 seconds after the sentence, and the full answer about 12 seconds later.

### Tiger Data

Tiger Data is Call Desk's stock-price stopwatch. When someone on a call says something important, we want to know: did the stock move because of it, or did similar companies move too? Every price goes into a TimescaleDB hypertable, price_ticks, which is a table built for data over time. It holds 1.46 million price rows: 40 stocks, every minute, for 30 days. The columnstore stores them 6.8× smaller (107.8 MB → 15.9 MB). A continuous aggregate with real-time aggregation turns them into 1-minute bars. One SQL query, timed at about 2.5 ms on Tiger, compares the stock's move after each sentence with a fund of similar companies (XLP for PepsiCo, JETS for Delta) and with rivals such as Coca-Cola. Each card shows the result in plain words, plus the query's speed. After PepsiCo's finance chief mentioned the lower profit forecast, PepsiCo did 0.91 percentage points worse than similar stocks over the next 30 minutes.

### Snowflake

Snowflake is Call Desk's memory of what each company said before the call. To know whether a forecast went up or down, you need the old forecast. Our Snowflake warehouse holds only forecasts published before the call, and no call transcripts. Cortex Search answers questions like "what did PepsiCo forecast for profit last quarter?" during the live check. A table of every earlier forecast, from December 2025 to July 2026, draws a forecast-history timeline on each card. After each answer, a Cortex Agent re-checks the claim on its own inside Snowflake, using Cortex Analyst over a semantic view, Cortex Search and SQL. The card shows its verdict (same as, changed from, or disagrees with the last forecast) and the SQL it wrote. If it agrees with the card, the confidence score goes up 3 points. If not, it drops 10. When the call ends, the cards are saved in Snowflake, and one AI_COMPLETE query writes a short call summary.

### Nimble

Nimble is Call Desk's web researcher. On a call, executives often mention a change without repeating the number. The number is in that morning's press release, and Nimble finds it live. For every card, a team of Nimble specialists runs at once. One searches only the company's own investor website and SEC filings, and returns the company's full forecast table in about half a second. Others search the wider web, Google results right now, and the company's site map. After the answer is on screen, Nimble's Web Search Agent re-checks the claim and returns a trust report with official sources and citations. On PepsiCo's profit forecast it rated the evidence SURE, with 7 official sources, in 17 seconds. Its answer also moves the confidence score: up 3 points if it agrees, down 10 if not. Other specialists read news, Reddit, and articles on sites with bot protection. If a specialist has no answer, the card says so, and the rest keep going.

### Sentry

Sentry is Call Desk's flight recorder: it records every step the AI takes, so anyone can check its work. Each card is its own AI agent trace (gen_ai.invoke_agent). Every Gemini call is a gen_ai.chat span. Every tool call (Snowflake, each Nimble specialist, Tiger Data, the Cortex Agent) is a gen_ai.execute_tool span in the same trace, with timings. The time from the spoken sentence to the card, and to the full answer, is recorded as a measurement. Sentry's browser SDK records each card appearing on screen inside the same trace, and every card links to its trace. Sentry also earned its place during the build. It grouped 11 timeouts in our fact-check agent, and the trace showed the Snowflake agent runs waiting in line behind each other. We gave each run its own connection, and three runs at once then finished in 22.7 seconds.

## Built with

Gemini API (gemini-3.8-flash, Gemini Live, Gemini TTS), Nimble, Tiger Data (TimescaleDB), Snowflake (Cortex Search, Cortex Agent, Cortex Analyst, AI_COMPLETE), Sentry, Python 3.11, FastAPI, Server-Sent Events, psycopg, pandas, vanilla JavaScript.

## AI tools used

Claude Code (Claude Opus 5.5) wrote and tested the code, researched the sponsor SDKs and prepared the demo data, under the team's direction. whisper.cpp located the demo clips offline. Inside the product: Gemini, plus the Snowflake Cortex models listed in the README's "Tools used" section.
