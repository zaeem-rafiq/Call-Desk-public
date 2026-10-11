# SDK notes for Call Desk (researched 2026-10-11)

These notes cover the Python 3.11 FastAPI app. Installed versions in `.venv`: google-genai 2.29.0, sentry-sdk 2.71.0,
snowflake-connector-python 4.8.0, psycopg 3.3.6 (binary impl), httpx 0.28.1, pydantic 2.14.0.

Tags used below:
- **[SRC]**: confirmed by reading or running the installed package in `.venv`.
- **[DOC]**: stated on the official documentation page cited for the section.
- **UNVERIFIED**: not confirmed by either. Treat it as a guess and test it before relying on it.

---

## 1. Google Gen AI SDK (`google-genai` 2.29.0) and the Gemini API

### Verified facts

**Model IDs (from the models and deprecations pages, as of 2026-10-11) [DOC]**

| Use | Model ID | Status |
|---|---|---|
| Fast audio understanding and transcription with a prompt | `gemini-3.8-flash` | Stable/GA (released 2026-09-02). Input: text/image/video/**audio**/PDF. Function calling and structured output are supported. Thinking levels: `low`, `medium` (default), `high`. `minimal` returns an error. |
| Dedicated speech-to-text (diarization, word timestamps) | `gemini-3.5-transcribe` (unary), `gemini-3.5-transcribe-live` (Live API) | Listed under stable Gemini 3 models. **No** function calling and **no** thinking. Unary limit is 1 h of audio, or 30 min with diarization or timestamps. Up to 8 speakers. |
| Stronger reasoning for the agent | `gemini-3.8-flash` with `thinking_level="high"` (GA), or `gemini-3.1-pro-preview` (**preview**, 1M in / 64k out, `minimal` unsupported) | Google positions 3.8 Flash as "most capable Flash" for agents. Pro remains preview only. `gemini-3-pro-preview` shut down on 2026-03-09. |
| Lite | `gemini-3.5-flash-lite` (stable, 2026-07-21) | `gemini-3.1-flash-lite` is also stable, with shutdown on 2027-05-07. Audio input is supported. |
| TTS (single and multi-speaker) | `gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts` | Stable (2026-09-22). Both support multi-speaker, voice design, and voice replication. |
| Live (realtime audio) | `gemini-3.8-live`, `gemini-3.8-live-extended-thinking` | Stable (2026-09-15). |

- **Deprecations [DOC]:** `gemini-2.5-flash-preview-tts`, `gemini-2.5-pro-preview-tts`, `gemini-3.1-flash-tts-preview`, `gemini-3.1-flash-live-preview`, and `gemini-2.5-flash-native-audio-preview-12-2025` all **shut down 2026-11-17**. `gemini-2.0-flash*` shut down on 2026-06-01. `gemini-3-flash-preview` is replaced by `gemini-3.6-flash`. Calls to `gemini-3.5-flash` and `gemini-3.7-flash` are auto-routed to newer models.
- **Gemini 2.5 [DOC]:** The 2.5 models are still served, but "access is limited to users who used them in the past". A new API key should not depend on any `gemini-2.5-*` ID.
- An alias `gemini-flash-latest` exists. Google hot-swaps it to each new release, with two weeks' email notice of breaking changes [DOC].
- **API surface [DOC]:** The docs now default to the **Interactions API** (`client.interactions.create`). Google says `generateContent` "remains fully supported" and recommends Interactions for new work. The `/generate-content/` doc pages carry a "(Legacy)" header. This app uses `client.aio.models.generate_content`, which is fine. Note that some doc examples are Interactions-only (`response_format=...`) and do not apply to `GenerateContentConfig` (see Gotchas).
- **Gemini 3.8 Flash migration notes [DOC]:**
  - Remove `temperature`, `top_p`, and `top_k` ("deprecated sampling parameters").
  - `candidate_count` is unsupported.
  - Use `thinking_level`, not `thinking_budget`. Sending both returns a 400 error.
  - For generateContent, "Ensure all `FunctionResponse` objects include `call_id` and `name`". In the Python SDK, that field is `FunctionResponse.id` [SRC].
- **Inline audio [DOC]:**
  - The **whole request must be ≤ 20 MB**, counting prompt, system instruction, and inline files. Use the Files API (`client.files.upload`) above that.
  - Accepted MIME types: `audio/wav`, `audio/mp3`, `audio/aiff`, `audio/aac`, `audio/ogg`, `audio/flac`, `audio/mpeg`, `audio/m4a`, `audio/l16`, `audio/opus`, `audio/alaw`, `audio/mulaw`, `audio/webm`.
  - Audio costs 32 tokens/s, and one prompt holds at most 9.5 h of audio.
- **Async client [SRC]:**
  - Call `client.aio.models.generate_content(model=, contents=, config=)`.
  - `genai.Client()` reads `GOOGLE_API_KEY` or `GEMINI_API_KEY`. If both are set, `GOOGLE_API_KEY` wins.
  - Close the async client with `await client.aio.aclose()`, or use `async with client.aio as ac:`.
- **Structured output [SRC]:** `GenerateContentConfig` has `response_mime_type`, `response_schema` (OpenAPI subset, accepts a Pydantic class), and `response_json_schema` (JSON Schema dict, e.g. `Model.model_json_schema()`).
  - Set only one of the two schema fields, together with `response_mime_type="application/json"`.
  - `response.parsed` is populated when `response_schema` is a Pydantic class.
  - Supported JSON-Schema keys include `$ref/$defs`, `type`, `enum`, `items`, `anyOf`, `properties`, `required`, `additionalProperties`, `minimum/maximum`, and `min/maxItems`.
- **Structured output with tools in one request [DOC]:** Allowed, but labeled a **preview feature** and **only for `gemini-3.1-pro-preview` and `gemini-3.8-flash`**. It covers Search, URL Context, Code Execution, File Search, and Function Calling. The function-calling page says mode `VALIDATED` is the default when built-in tools or structured outputs are also enabled.
- **Function calling [SRC+DOC]:**
  - `FunctionDeclaration` fields: `name`, `description`, `parameters` (OpenAPI `Schema`), `parameters_json_schema` (JSON Schema dict), `response`, `response_json_schema`, `behavior`.
  - `FunctionDeclaration.from_callable(client=, callable=, behavior=None, use_json_schema=False)`.
  - When you pass plain Python callables in `tools`, automatic function calling (AFC) runs them. Disable it with `automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)`.
  - `response.function_calls -> list[FunctionCall] | None`. `FunctionCall` has `id`, `name`, and `args`. The doc says the ID is "now always returned by the API for Gemini 3 models".
  - Modes (`FunctionCallingConfigMode`): `AUTO`, `ANY`, `NONE`, `VALIDATED`, plus `allowed_function_names`.
  - **Thought signatures are mandatory for function calling.** Append the model's `response.candidates[0].content` unchanged to history. Do not merge or split signed parts.
- **Timeouts and retries [SRC]:**
  - `types.HttpOptions(timeout=...)` is in **milliseconds**.
  - Retries are **off by default**: `retry_options=None` means `stop_after_attempt(1)`.
  - `types.HttpRetryOptions()` defaults: `attempts=5` (including the first try), `initial_delay=1.0` s, `max_delay=60.0` s, `exp_base=2`, `jitter=1`, `http_status_codes=(408,429,500,502,503,504)`, plus httpx transient errors.
  - Set these on the `Client(http_options=...)` or per request through `GenerateContentConfig(http_options=...)`.
- **TTS [DOC+SRC]:**
  - Text in, audio out.
  - Multi-speaker allows **at most 2 speakers** with prebuilt voices.
  - **3.8 TTS models return a complete WAV (RIFF header) by default**: 24 kHz, mono, 16-bit signed LE. The older `gemini-3.1-flash-tts-preview` returned headerless PCM. Streaming returns headerless `audio/l16`.
  - Per-turn speaker and style go in `Part.speech_metadata = SpeechMetadata(speaker=, style=)` [SRC: field exists in 2.29.0].
  - 30 voices: Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede, Callirrhoe, Autonoe, Enceladus, Iapetus, Umbriel, Algieba, Despina, Erinome, Algenib, Rasalgethi, Laomedeia, Achernar, Alnilam, Schedar, Gacrux, Pulcherrima, Achird, Zubenelgenubi, Vindemiatrix, Sadachbia, Sadaltager, Sulafat.
  - SDK classes [SRC]: `SpeechConfig(voice_config|multi_speaker_voice_config, language_code)`, `MultiSpeakerVoiceConfig(speaker_voice_configs)`, `SpeakerVoiceConfig(speaker, voice_config)`, `VoiceConfig(prebuilt_voice_config|replicated_voice_config|voice)`, `PrebuiltVoiceConfig(voice_name)`.
- **Transcription config [SRC+DOC]:** `GenerateContentConfig.audio_transcription_config = types.AudioTranscriptionConfig(language_codes=[...], custom_vocabulary=[...], word_timestamp=bool, diarization=bool, mode="VERBATIM"|"SMART")`.
  - `response.text` holds the transcript.
  - Speaker labels and words are in `part.audio_transcription.speaker_label` and `.words[].word/start_offset/end_offset`.
  - `custom_vocabulary` cannot be combined with diarization or timestamps. `SMART` cannot be combined with timestamps or diarization.

### Code: client, inline audio, structured output (async)

```python
import asyncio
from google import genai
from google.genai import types
from pydantic import BaseModel

client = genai.Client(http_options=types.HttpOptions(
    timeout=60_000,                                     # milliseconds
    retry_options=types.HttpRetryOptions(attempts=3),   # retries 408/429/5xx; off if omitted
))

class CallSummary(BaseModel):
    transcript: str
    intent: str
    urgent: bool

async def understand_call(audio: bytes, mime: str = "audio/wav") -> CallSummary:
    resp = await client.aio.models.generate_content(
        model="gemini-3.8-flash",
        contents=[types.Part.from_bytes(data=audio, mime_type=mime),   # whole request <= 20 MB
                  "Transcribe this call verbatim, then classify the caller's intent."],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=CallSummary.model_json_schema(),  # or response_schema=CallSummary -> resp.parsed
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    return CallSummary.model_validate_json(resp.text)

# Dedicated ASR with diarization (docs use client.files.upload; inline bytes here are UNVERIFIED but use the same Part type)
async def diarize(audio: bytes) -> list[dict]:
    resp = await client.aio.models.generate_content(
        model="gemini-3.5-transcribe",
        contents=[types.Part.from_bytes(data=audio, mime_type="audio/mp3")],
        config=types.GenerateContentConfig(audio_transcription_config=types.AudioTranscriptionConfig(
            diarization=True, word_timestamp=True)),
    )
    out = []
    for part in resp.candidates[0].content.parts:
        if part.audio_transcription:
            for w in part.audio_transcription.words or []:
                out.append({"speaker": part.audio_transcription.speaker_label, "word": w.word,
                            "start": w.start_offset, "end": w.end_offset})
    return out
```

### Code: manual function-calling loop (AFC off, parallel calls, IDs kept)

```python
TOOLS = {"lookup_customer": lookup_customer, "create_ticket": create_ticket}  # your async functions

DECLS = [
    types.FunctionDeclaration(
        name="lookup_customer", description="Find a customer by phone number.",
        parameters_json_schema={"type": "object", "properties": {"phone": {"type": "string"}},
                                "required": ["phone"]}),
    types.FunctionDeclaration(
        name="create_ticket", description="Open a support ticket.",
        parameters_json_schema={"type": "object", "properties": {
            "summary": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "high"]}},
            "required": ["summary", "priority"]}),
]
AGENT_CONFIG = types.GenerateContentConfig(
    system_instruction="You are the Call Desk triage agent.",
    tools=[types.Tool(function_declarations=DECLS)],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
        mode="AUTO")),  # "ANY" forces a call; add allowed_function_names=[...] to restrict
    thinking_config=types.ThinkingConfig(thinking_level="high"),
)

async def run_agent(user_text: str, max_steps: int = 6) -> str:
    contents = [types.Content(role="user", parts=[types.Part(text=user_text)])]
    for _ in range(max_steps):
        resp = await client.aio.models.generate_content(
            model="gemini-3.8-flash", contents=contents, config=AGENT_CONFIG)
        calls = resp.function_calls or []
        if not calls:
            return resp.text or ""
        contents.append(resp.candidates[0].content)          # keeps thought_signature parts intact
        results = await asyncio.gather(*(TOOLS[c.name](**(c.args or {})) for c in calls),
                                       return_exceptions=True)
        contents.append(types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id=c.id, name=c.name,                          # id is required for Gemini 3.8 generateContent
                response={"error": str(r)} if isinstance(r, Exception) else {"output": r}))
            for c, r in zip(calls, results)]))
    raise RuntimeError("agent exceeded max_steps")
```

### Code: multi-speaker TTS to a .wav file

```python
import wave

async def synthesize(turns: list[tuple[str, str]], path: str) -> None:   # [("Agent","Hi..."),("Caller","...")]
    voices = {"Agent": "Kore", "Caller": "Puck"}                            # max 2 speakers per request
    resp = await client.aio.models.generate_content(
        model="gemini-3.8-flash-tts",
        contents=[types.Content(role="user", parts=[
            types.Part(text=t, speech_metadata=types.SpeechMetadata(speaker=s)) for s, t in turns])],
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                speaker_voice_configs=[types.SpeakerVoiceConfig(
                    speaker=s, voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=v)))
                    for s, v in voices.items()])),
        ),
    )
    data = resp.candidates[0].content.parts[0].inline_data.data
    if data[:4] == b"RIFF":                    # 3.8 TTS default: complete WAV, write as-is
        with open(path, "wb") as f:
            f.write(data)
    else:                                      # legacy preview TTS / streaming: raw PCM s16le 24 kHz mono
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000); wf.writeframes(data)
```

### Gotchas
- **`types.Part.from_function_response(..., id=...)` raises `TypeError` in 2.29.0 [SRC].** The signature is `(*, name, response, parts=None)`. The docs show `id=`, which appears to belong to a newer SDK. Build `types.Part(function_response=types.FunctionResponse(id=..., name=..., response=...))` instead, as shown above.
- **`GenerateContentConfig(response_format=...)` fails validation (`extra_forbidden`) in 2.29.0 [SRC].** The structured-output doc page shows `response_format={"text": {...}}`, but that is not a 2.29.0 field. The TTS page's `response_format.audio.mime_type` is not a 2.29.0 field either. Use `response_mime_type` with `response_json_schema` (or `response_schema`).
- Do not set `temperature` on 3.8 Flash. Do not send `thinking_level="minimal"` to 3.8 Flash or 3.1 Pro, because it errors.
- Structured output with function calling is a preview feature, so expect edge cases. The safest pattern is to run the tool loop without a schema, then make one final call with `response_json_schema` and no tools.
- `speech_metadata` per part is the documented way for 3.8 TTS. Whether older "Joe: …\nJane: …" plain-text transcripts still work on 3.8 TTS is UNVERIFIED.
- `gemini-3.5-transcribe` cannot call tools or think. Whether it accepts a text prompt, system instruction, or response schema is UNVERIFIED. Use `gemini-3.8-flash` when you need transcript plus extraction in one call.
- Sentry's `GoogleGenAIIntegration` monkeypatches `Models/AsyncModels.generate_content`, so LLM spans appear automatically (see section 5).

### Live API (stretch goal, one paragraph)
Use `gemini-3.8-live` for realtime audio. `gemini-3.5-transcribe-live` is the transcription-only option, with no diarization or timestamps and up to 10 min per session. Input must be raw 16-bit PCM, little-endian, 16 kHz, with mime `audio/pcm;rate=16000`. Audio-only sessions are limited to 15 min. Input transcription is enabled with `config={"response_modalities": ["AUDIO"], "input_audio_transcription": {}}`, and output transcription with `"output_audio_transcription": {}`. Read transcripts from `msg.server_content.input_transcription.text` and `output_transcription.text`. `LiveConnectConfig` in 2.29.0 has both fields [SRC], and the entry point is `client.aio.live.connect(model=..., config=...)` [SRC]. The plain `generateContent` audio docs say they do not do real-time transcription.

### Sources
https://ai.google.dev/gemini-api/docs/models · https://ai.google.dev/gemini-api/docs/deprecations · https://ai.google.dev/gemini-api/docs/latest-model · https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash · https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite · https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe · https://ai.google.dev/gemini-api/docs/generate-content/transcribe · https://ai.google.dev/gemini-api/docs/generate-content/audio · https://ai.google.dev/gemini-api/docs/generate-content/structured-output · https://ai.google.dev/gemini-api/docs/generate-content/function-calling · https://ai.google.dev/gemini-api/docs/generate-content/speech-generation · https://ai.google.dev/gemini-api/docs/gemini-3 · https://ai.google.dev/gemini-api/docs/live-guide · https://ai.google.dev/gemini-api/docs/migrate-to-interactions · source: `.venv/.../google/genai/types.py`, `_api_client.py` (`retry_args`), `client.py`

---

## 2. Nimble (nimbleway.com) web search and extraction

### Verified facts [DOC]
- **Base URL:** `https://sdk.nimbleway.com/v2`.
- **Auth headers:** `Authorization: Bearer <NIMBLE_API_KEY>` and `Content-Type: application/json`.
- **Search: `POST /v2/search`**. Body fields:
  - `query` (required).
  - `max_results` (1–100, default 10).
  - `search_depth`: `lite` returns title, URL, and description only. `standard` is the default and returns rich content.
  - `full_content` (bool, default false).
  - `focus`: `general` by default. Other values: `news`, `coding`, `academic`, `shopping`, `social`, `geo`, `location`. Focus modes are documented as working only with `lite`.
  - `country` (default `US`), `locale` (default `en`).
  - `output_format`: `markdown` (default), `plain_text`, or `simplified_html`.
  - `time_range` (`hour|day|week|month|year`), or `start_date`/`end_date`. These cannot be combined.
  - `include_domains`/`exclude_domains` (max 50 each), `content_type`.
- **Search response:** `{"request_id", "total_results", "results": [{"title", "description", "url", "content", "metadata": {...}, "additional_data"?}]}`.
  - The snippet is in **`description`**.
  - `content` is empty unless the depth is `standard` or `full_content` is set.
  - SERP metadata has `position`, `entity_type`, `country`, `locale`. Agent metadata has `agent_name`.
  - There is no answer or summary field.
- **Fast SERP (realtime Google): `POST /v2/fast-serp`**.
  - Body: `search_engine` (`google_search|google_news|google_images`, required), `query`, `num_results` (1–100, default 10), `page`, `location`, `parse` (default true), `render` (default false), `device`, `resolve_url`.
  - Results are at `parsing.entities.OrganicResult[]` with `position`, `title`, `url`, `snippet`, `cleaned_domain`, `displayed_url`.
  - Latency: **p50 < 700 ms, p90 < 1000 ms**. Rate limit: 10 QPS by default. Country and locale are fixed to US / en-US.
- **Extract: `POST /v2/extract`** (synchronous, one POST returns the page).
  - Body: `url` (required), `formats` (array: `html`, `markdown`, `screenshot`, `headers`, `links`), `render` (bool or `"auto"`).
  - Also: `driver` (`vx6`… `vx12-pro`, `auto`), `country` (default `ALL`), `locale`, `markdown_backend` (`full_page` default, or `main_content`), `request_timeout` (ms), `browser_actions`, `parse`/`parser`, `headers`, `cookies`.
  - Response: `{"task_id", "url", "status", "status_code", "data": {"html", "markdown", "links", ...}, "metadata": {"query_duration" (ms), "driver", ...}, "warnings"?}`.
  - `status` can be `success`, `skipped`, `fatal`, `error`, `postponed`, `ignored`, `rejected`, or `blocked`.
  - Async and batch variants exist (`extract-async`, `extract-batch`).
- **Latency:** `/v2/search` publishes **no latency figures** (UNVERIFIED, so measure it). `full_content` "adds latency" because pages are scraped live. The extract doc's sample response shows `query_duration: 1877` ms.
- **Rate limits:** 83 QPS (5,000 QPM) per account by default. A 429 returns the body `{"status":"failed","msg":"Rate limit exceeded"}`. A 402 means the trial quota is finished.
- **Pricing:** Search `lite` costs $1.10 per 1K and `standard` $5.00 per 1K. `full_content` adds $1.00 per 1K URLs. Extraction tools cost $1.00 per 1K URLs. The free trial is 5,000 pages with no card needed.
- **Python SDK:** `pip install nimble_python` (PyPI 1.6.0). PyPI describes it as "The official Python library for the nimble API", with repo `github.com/Nimbleway/nimble-python` and author `support@nimbleway.com`.
  - Usage: `from nimble_python import Nimble, AsyncNimble`, then `Nimble(api_key=...)`.
  - Methods: `.search(query=..., max_results=...)` and `.extract.run(url=..., render=True)`.
  - SDK defaults: 2 retries and a 3-minute timeout.
  - The SDK is official, but plain httpx (already installed) avoids a new dependency. Use httpx.

### Code: curl

```bash
curl -sS -X POST https://sdk.nimbleway.com/v2/search \
  -H "Authorization: Bearer $NIMBLE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"acme corp phone scam reports","max_results":5,"search_depth":"lite"}'

curl -sS -X POST https://sdk.nimbleway.com/v2/extract \
  -H "Authorization: Bearer $NIMBLE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.example.com","formats":["markdown"],"markdown_backend":"main_content"}'
```

### Code: httpx (async)

```python
import os, httpx

nimble = httpx.AsyncClient(
    base_url="https://sdk.nimbleway.com/v2",
    headers={"Authorization": f"Bearer {os.environ['NIMBLE_API_KEY']}"},
    timeout=httpx.Timeout(30.0, connect=5.0),
)

async def nimble_search(query: str, n: int = 5) -> list[dict]:
    r = await nimble.post("/search", json={"query": query, "max_results": n, "search_depth": "lite"})
    r.raise_for_status()
    return [{"title": x["title"], "url": x["url"], "snippet": x["description"]} for x in r.json()["results"]]

async def nimble_extract(url: str) -> str:
    r = await nimble.post("/extract", json={"url": url, "formats": ["markdown"],
                                            "markdown_backend": "main_content", "request_timeout": 20000})
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"nimble extract {body.get('status')}: {body.get('status_code')}")
    return body["data"].get("markdown") or ""
```

### Gotchas
- The default `search_depth` is `standard`, which costs about 4.5 times `lite`. Pass `"lite"` when snippets are enough.
- Always pass `formats`. A third-party review says extract returns only HTML without it. That claim is UNVERIFIED, but passing the field costs nothing.
- `request_timeout` is in milliseconds.
- Do not confuse this with the separate `nimbleway` pip package mentioned on an older "client libraries" page. The current docs use `nimble_python`.
- No "agentic answer" in one synchronous call exists. Web Search Agents (`POST /v2/agents/runs`) are async, require polling or SSE, and their response shape is not documented on the overview page (UNVERIFIED).

### Sources
https://docs.nimbleway.com · https://docs.nimbleway.com/nimble-sdk/web-tools/search · https://docs.nimbleway.com/api-reference/search/search.md · https://docs.nimbleway.com/nimble-sdk/web-tools/search-depth.md · https://docs.nimbleway.com/nimble-sdk/web-tools/fast-serp.md · https://docs.nimbleway.com/nimble-sdk/web-tools/extract/quickstart · https://docs.nimbleway.com/api-reference/extract/extract.md · https://docs.nimbleway.com/nimble-sdk/admin/rate-limits.md · https://docs.nimbleway.com/nimble-sdk/admin/pricing.md · https://docs.nimbleway.com/nimble-sdk/sdks/python.md · https://pypi.org/project/nimble_python/

---

## 3. Snowflake (connector 4.8.0) and Cortex Search

### Verified facts
- **Account identifier [DOC]:** Use `"<orgname>-<account_name>"` (preferred) **without** `.snowflakecomputing.com`. The legacy form is `<locator>.<region>.<cloud>`, e.g. `xy12345.us-east-2.aws`. Find the values in Snowsight under account selector → View account details, or run `SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME();`.
- **`connect()` keys [SRC]:** `account`, `user`, `password`, `warehouse`, `database`, `schema`, `role`, `authenticator`, `token`, `token_file_path`, `private_key_file`, `login_timeout`, `network_timeout`, `socket_timeout`, `client_session_keep_alive`.
  - `login_timeout` is in seconds. Its default is 120 (`DEFAULT_AUTH_CLASS_TIMEOUT`).
  - `network_timeout` is in seconds. Its default is None, which means **infinite**.
- **PAT with the connector [SRC]:** 4.8.0 accepts `authenticator="PROGRAMMATIC_ACCESS_TOKEN", token="<pat>"`, and `user` may then be empty. The PAT doc's own example passes the PAT as `password=` [DOC]. Both forms work.
- **Password-only auth for humans [DOC]:**
  - Snowflake is rolling out single-factor password deprecation in three phases:
    1. Snowsight MFA (Sep 2025–Jan 2026).
    2. New human users need MFA everywhere (May–Jul 2026).
    3. All human users need MFA and `LEGACY_SERVICE` users are blocked from passwords (Aug–Oct 2026, rolling per account).
  - **However: "The phases described in this topic don't apply to reader accounts, trial accounts, or Snowflake Postgres."**
  - So a **trial** account can still log a script in with user and password today. Plan for a PAT anyway, because the account converts to paid eventually.
- **PAT rules [DOC]:**
  - Create one in Snowsight under Governance & security » Users & roles → user → Programmatic access tokens → Generate, or with SQL `ALTER USER ... ADD PROGRAMMATIC ACCESS TOKEN <name> [ROLE_RESTRICTION='<role>'] [DAYS_TO_EXPIRY=n]`.
  - The secret is shown **once**. Expiry is 15 days by default and 365 at most.
  - **A PERSON user can generate a PAT without a network policy but must be subject to a network policy to authenticate with it.** You can remove that requirement with an authentication policy that sets `PAT_POLICY=(NETWORK_POLICY_EVALUATION = ENFORCED_NOT_REQUIRED)`.
  - SERVICE users need a network policy to generate or use a PAT, and need `ROLE_RESTRICTION`.
- **Async [SRC]:** `snowflake.connector.aio` exists, but its README says "Preview feature … not supported by Snowflake Support" and it needs `aiohttp`, which is not installed. Use the sync connector inside `asyncio.to_thread`.
- **Cortex Search DDL [DOC]:**
  - Syntax: `CREATE [OR REPLACE] CORTEX SEARCH SERVICE [IF NOT EXISTS] <name> ON <text_col> [PRIMARY KEY (...)] ATTRIBUTES <col,...> WAREHOUSE = <wh> TARGET_LAG = '<n> <unit>' [EMBEDDING_MODEL = '<model>'] [REFRESH_MODE = FULL|INCREMENTAL] [INITIALIZE = ON_CREATE|ON_SCHEDULE] [AUTO_SUSPEND = <secs≥1800>] [COMMENT='..'] AS <query>`.
  - The multi-index form uses `TEXT INDEXES ... VECTOR INDEXES ...` instead.
  - The default embedding model is `snowflake-arctic-embed-m-v1.5` (English, 768-d). Others: `snowflake-arctic-embed-l-v2.0` (multilingual), `snowflake-arctic-embed-l-v2.0-8k`, and `voyage-multilingual-2` (some regions only). The model **cannot be changed after creation**.
  - `INITIALIZE=ON_CREATE` (the default) builds the index synchronously inside the CREATE. It "may take up to several hours" for large data. A small knowledge base should take minutes, but that is UNVERIFIED.
  - Use a dedicated warehouse no larger than MEDIUM.
  - Incremental refresh needs change tracking. Snowflake tries to enable it automatically.
- **Privileges [DOC]:**
  - To create: `CREATE CORTEX SEARCH SERVICE` on the schema, `SELECT` on the source tables, `USAGE` on the warehouse, and database role `SNOWFLAKE.CORTEX_USER` or `SNOWFLAKE.CORTEX_EMBED_USER`.
  - To query: `USAGE` on the database, the schema, and the service.
  - On a trial, ACCOUNTADMIN has all of these.
- **Regions [DOC]:**
  - Native AWS US regions: US West 2 (Oregon), US East 1 (N. Virginia), US East 2 (Ohio).
  - Some regions (AWS Paris, Zurich, Singapore, Osaka; Azure Canada Central, Central US, UAE North) support Cortex Search **only via cross-region inference**. Turn it on with `ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US';`. It is ACCOUNTADMIN only, set at account level only, and accepts `ANY_REGION`, `AWS_US`, `DISABLED`, and similar values.
  - No trial-specific Cortex restriction is documented (UNVERIFIED). Check with `SHOW PARAMETERS LIKE 'CORTEX_ENABLED_CROSS_REGION' IN ACCOUNT;`.
- **Querying from Python with only the connector [DOC]:**
  - Call `SNOWFLAKE.CORTEX.SEARCH_PREVIEW('<db.schema.service>', '<json>')`.
  - JSON fields: `query` (required), `columns`, `filter` (`@eq`, `@contains`, `@gte`, `@lte`, `@primarykey`, `@and`, `@or`, `@not` on ATTRIBUTES columns), `limit` (default 10), and `scoring_config`.
  - Returns `{"results": [{<col>: <val>, ...}], "request_id": "..."}`.
  - Results are **truncated at 300 KB**, and only **constant/string-literal** arguments are accepted.
  - The docs call it "designed for testing and validation". It is slower than REST or Python, but it is fine for a hackathon.
- **REST [DOC]:** `POST https://<account_url>/api/v2/databases/<db>/schemas/<schema>/cortex-search-services/<svc>:query`.
  - Headers: `Authorization: Bearer <PAT>`, `Content-Type`, and `Accept: application/json`.
  - The body is the same JSON as above. `limit` goes up to 1000, and responses up to 10 MB.
- **`snowflake.core` [DOC]:** `Root(session).databases[db].schemas[s].cortex_search_services[name].search(query=, columns=, filter=, limit=)`. It needs a Snowpark `Session`. Neither package is installed. Skip it.
- **Costs [DOC]:** Warehouse credits for refresh and embedding, EMBED_TEXT tokens, and **serving compute billed per GB-month of indexed data even when idle**. Drop the service or set `AUTO_SUSPEND` after the demo.

### What a new trial user must do for non-interactive script login
1. Note the account identifier `orgname-account_name` from View account details.
2. **Option A (simplest on a trial):** use `user` and `password` directly. Trials are exempt from the MFA phases [DOC].
3. **Option B (survives MFA enforcement):** generate a PAT (Snowsight path above). Copy it immediately. Then do one of the following:
   - Attach a network policy: `CREATE NETWORK POLICY calldesk_np ALLOWED_IP_LIST=('0.0.0.0/0'); ALTER USER <me> SET NETWORK_POLICY = calldesk_np;`. This is standard Snowflake SQL, not re-checked today.
   - Or attach an authentication policy with `PAT_POLICY=(NETWORK_POLICY_EVALUATION=ENFORCED_NOT_REQUIRED)` using `ALTER USER <me> SET AUTHENTICATION POLICY <db.schema.policy>;`. The ALTER USER syntax is UNVERIFIED today.
4. Key-pair (`authenticator="SNOWFLAKE_JWT"`, `private_key_file=`) with a `TYPE=SERVICE` user is the long-term option.

### Code

```sql
-- one-time setup (run as ACCOUNTADMIN in a worksheet)
CREATE WAREHOUSE IF NOT EXISTS CALLDESK_WH WAREHOUSE_SIZE='XSMALL' AUTO_SUSPEND=60 AUTO_RESUME=TRUE;
CREATE DATABASE IF NOT EXISTS CALLDESK; CREATE SCHEMA IF NOT EXISTS CALLDESK.KB;
CREATE TABLE IF NOT EXISTS CALLDESK.KB.ARTICLES (ID TEXT, TITLE TEXT, BODY TEXT, CATEGORY TEXT);
CREATE OR REPLACE CORTEX SEARCH SERVICE CALLDESK.KB.KB_SEARCH
  ON BODY
  ATTRIBUTES CATEGORY
  WAREHOUSE = CALLDESK_WH
  TARGET_LAG = '1 hour'
  EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0'
  AS (SELECT ID, TITLE, BODY, CATEGORY FROM CALLDESK.KB.ARTICLES);
```

```python
import asyncio, json, os
import snowflake.connector

def sf_connect():
    common = dict(account=os.environ["SNOWFLAKE_ACCOUNT"],        # "orgname-account_name"
                  user=os.environ["SNOWFLAKE_USER"], role=os.environ.get("SNOWFLAKE_ROLE"),
                  warehouse="CALLDESK_WH", database="CALLDESK", schema="KB",
                  login_timeout=20, network_timeout=30)               # seconds
    if pat := os.environ.get("SNOWFLAKE_PAT"):
        return snowflake.connector.connect(**common, authenticator="PROGRAMMATIC_ACCESS_TOKEN", token=pat)
    return snowflake.connector.connect(**common, password=os.environ["SNOWFLAKE_PASSWORD"])

def _search(query: str, limit: int, category: str | None) -> list[dict]:
    q = {"query": query, "columns": ["ID", "TITLE", "BODY"], "limit": limit}
    if category:
        q["filter"] = {"@eq": {"CATEGORY": category}}
    with sf_connect() as conn, conn.cursor() as cur:
        # default paramstyle is client-side 'pyformat': values are inlined as literals, which
        # SEARCH_PREVIEW requires (UNVERIFIED end-to-end; if it errors, inline an escaped literal)
        cur.execute("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)", ("CALLDESK.KB.KB_SEARCH", json.dumps(q)))
        return json.loads(cur.fetchone()[0])["results"]

async def kb_search(query: str, limit: int = 5, category: str | None = None) -> list[dict]:
    return await asyncio.to_thread(_search, query, limit, category)
```

### Gotchas
- Column names in `columns` and `filter` must match the service's source query. Unquoted identifiers are upper-cased, so the safest choice is to use UPPERCASE names everywhere.
- `CREATE OR REPLACE` rebuilds the index, and the old data is unavailable during the rebuild.
- Do not put PII in Cortex Search metadata fields [DOC].
- Opening one connection per search adds login latency. Reuse one connection per worker thread if latency matters.

### Sources
https://docs.snowflake.com/en/user-guide/admin-account-identifier · https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect · https://docs.snowflake.com/en/user-guide/programmatic-access-tokens · https://docs.snowflake.com/en/user-guide/security-mfa-rollout · https://docs.snowflake.com/en/sql-reference/sql/create-cortex-search · https://docs.snowflake.com/en/sql-reference/functions/search_preview-snowflake-cortex · https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/cortex-search-overview · https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/query-cortex-search-service · https://docs.snowflake.com/en/user-guide/snowflake-cortex/cross-region-inference · source: `.venv/.../snowflake/connector/connection.py`, `auth/pat.py`, `auth/by_plugin.py`, `aio/README.md`

---

## 4. Tiger Data (TimescaleDB on Tiger Cloud) with psycopg 3.3.6

### Verified facts [DOC]
- **Connection string** (printed by `tiger service create`): `postgresql://tsdbadmin:<password>@<service-id>.<project-id>.tsdb.cloud.timescale.com:<port>/tsdb?sslmode=require`.
  - The default SSL mode is `require`. `verify-ca`/`verify-full` need the cert chain.
  - "Free services do not supply SSL certificates", so keep `require` on the free tier.
- **Hypertable, new syntax (TimescaleDB ≥ 2.20):** `CREATE TABLE ... WITH (tsdb.hypertable, tsdb.partition_column='<col>', tsdb.chunk_interval='<interval>', tsdb.segmentby='<col>', tsdb.orderby='<col> DESC', ...)`.
  - The defaults are: partition column = first `TIMESTAMP`/`TIMESTAMPTZ` column, `chunk_interval` = 7 days, `columnstore` = true.
  - The command **auto-creates a columnstore policy**, which converts chunks after one chunk interval on a 1-day schedule.
- **Hypertable, older syntax (2.13+):** `SELECT create_hypertable('t', by_range('ts'), if_not_exists => TRUE);` on an existing table. It is still supported.
- **Continuous aggregate syntax:** `CREATE MATERIALIZED VIEW v WITH (timescaledb.continuous [, timescaledb.materialized_only = true|false, timescaledb.chunk_interval=..., timescaledb.create_group_indexes=...]) AS SELECT time_bucket(<interval>, <time_col>) AS bucket, ... GROUP BY bucket, ... [WITH [NO] DATA];`.
  - `WITH DATA` is the default and materializes everything at creation.
  - **"To create a continuous aggregate within a transaction block, use the WITH NO DATA option."**
  - From 2.7 on, all PostgreSQL aggregates work, and `first(value, time)`/`last(value, time)` appear in the official examples.
  - Window functions are experimental and off by default.
- **Real-time aggregation:** "In TimescaleDB v2.13 and later, real-time aggregates are DISABLED by default", because `timescaledb.materialized_only` defaults to TRUE. Enable it with `ALTER MATERIALIZED VIEW v SET (timescaledb.materialized_only = false);` or in the CREATE `WITH` clause.
- **`refresh_continuous_aggregate`** is a **procedure**: `CALL refresh_continuous_aggregate('<cagg>', window_start, window_end [, force => false, options => '{...}'])`.
  - `NULL` bounds mean the lowest or highest changed element.
  - It **cannot run inside a transaction block**. The error is `refresh_continuous_aggregate() cannot run inside a transaction block`. This comes from user error reports. The reference page does not state it but says each batch commits separately. With psycopg, use an **autocommit** connection.
- **`add_continuous_aggregate_policy`**(`continuous_aggregate`, `start_offset`, `end_offset`, `schedule_interval` default 24 h, `if_not_exists` default false, `initial_start`, `timezone`, `include_tiered_data`, `buckets_per_batch`=10, `max_batches_per_execution`=0, `refresh_newest_first`=true). It returns a job_id, and `start_offset` must be greater than `end_offset`.
  - A "window must cover ≥ 2 buckets" rule is not stated on the page (UNVERIFIED). Keep the window much wider than one bucket.
- **Tiger CLI (document only, not installed):**
  - Install with `brew install --cask timescale/tap/tiger-cli` or `curl -fsSL https://cli.tigerdata.com | sh`.
  - Log in with `tiger auth login`, which uses browser OAuth. `--headless` uses a device code. For CI, use `--public-key/--secret-key` or `TIGER_PUBLIC_KEY`/`TIGER_SECRET_KEY`.
  - `tiger service create [--name ..] [--cpu shared --memory shared --region us-east-1]` creates a service. Free services are beta and limited to us-east-1.
  - Other commands: `tiger service list`, `tiger db uri <service-id> [--pooled] [--with-password]` (`tiger db connection-string` still works as an alias), and `tiger db psql <service-id>`.

### Code

```sql
CREATE TABLE IF NOT EXISTS call_flags (
  ts       TIMESTAMPTZ NOT NULL,
  call_id  TEXT        NOT NULL,
  flag     TEXT        NOT NULL,
  score    DOUBLE PRECISION
) WITH (tsdb.hypertable, tsdb.partition_column = 'ts', tsdb.chunk_interval = '1 day',
        tsdb.segmentby = 'flag', tsdb.orderby = 'ts DESC');
-- Older equivalent: CREATE TABLE ... ; SELECT create_hypertable('call_flags', by_range('ts'), if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS call_flags_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket, flag,
       count(*) AS n, avg(score) AS avg_score,
       first(score, ts) AS first_score, last(score, ts) AS last_score
FROM call_flags
GROUP BY bucket, flag
WITH NO DATA;

SELECT add_continuous_aggregate_policy('call_flags_1m',
  start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',
  schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);
```

```python
import os, psycopg

DSN = os.environ["TIGER_DATABASE_URL"]   # ...tsdb.cloud.timescale.com:<port>/tsdb?sslmode=require

async def migrate(ddl_statements: list[str]) -> None:
    # autocommit=True: required for CALL refresh_continuous_aggregate and for CREATE ... WITH DATA
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        for stmt in ddl_statements:
            await conn.execute(stmt)
        await conn.execute("CALL refresh_continuous_aggregate('call_flags_1m', NULL, NULL)")

async def insert_flag(conn: psycopg.AsyncConnection, call_id: str, flag: str, score: float) -> None:
    await conn.execute("INSERT INTO call_flags (ts, call_id, flag, score) VALUES (now(), %s, %s, %s)",
                       (call_id, flag, score))
```

### Gotchas
- psycopg 3 defaults to `autocommit=False`, which wraps every statement in a transaction. That **breaks `CALL refresh_continuous_aggregate`**. Use `autocommit=True` or `await conn.set_autocommit(True)`.
- `IF NOT EXISTS` combined with `WITH (tsdb.hypertable)` and on `CREATE MATERIALIZED VIEW ... (timescaledb.continuous)` is UNVERIFIED. Test both, or guard them with a catalog check.
- With real-time enabled, the newest un-materialized buckets are computed live from raw rows. That is correct for a live dashboard, but slower as the gap grows.
- With the pooled connection (`--pooled`), set `prepare_threshold=None` in `psycopg.AsyncConnection.connect(...)`. Transaction-mode poolers break prepared statements. That Tiger's pooler runs in transaction mode is UNVERIFIED.

### Sources
https://www.tigerdata.com/docs/reference/timescaledb/hypertables/create_table/ · https://www.tigerdata.com/docs/reference/timescaledb/hypertables/create_hypertable/ · https://www.tigerdata.com/docs/reference/timescaledb/continuous-aggregates/create_materialized_view/ · https://www.tigerdata.com/docs/reference/timescaledb/continuous-aggregates/refresh_continuous_aggregate/ · https://www.tigerdata.com/docs/reference/timescaledb/continuous-aggregates/add_continuous_aggregate_policy/ · https://www.tigerdata.com/docs/build/continuous-aggregates/create-a-continuous-aggregate · https://www.tigerdata.com/docs/learn/continuous-aggregates · https://www.tigerdata.com/docs/learn/continuous-aggregates/real-time-aggregates · https://www.tigerdata.com/docs/get-started/quickstart/tiger-cli · https://www.tigerdata.com/docs/reference/tiger-cloud/tiger-cli · https://www.tigerdata.com/docs/deploy/tiger-cloud/tiger-cloud-aws/security/strict-ssl · https://community.grafana.com/t/postgres-queries-without-a-transaction/56435 (transaction-block error)

---

## 5. Sentry Python (sentry-sdk 2.71.0) AI agent monitoring

### Verified facts
- **`GoogleGenAIIntegration` exists in 2.71.0 and is auto-enabling [SRC].**
  - It lives at `sentry_sdk/integrations/google_genai/` and is listed in `_AUTO_ENABLING_INTEGRATIONS`, with google-genai ≥ 1.29.0 required.
  - A test `init()` in this venv showed active integrations: `fastapi`, `starlette`, `httpx`, `google_genai`, and others.
  - Constructor: `GoogleGenAIIntegration(include_prompts=True)`.
- **What it instruments automatically [SRC]:**
  - Sync and async `models.generate_content`, `generate_content_stream`, and `embed_content` become spans with op `gen_ai.chat` and name `chat <model>`, or op `gen_ai.embeddings`.
  - It sets `gen_ai.system="gcp.gemini"`, request model and params, response model/id/finish reasons/tool calls, and token usage (`gen_ai.usage.input_tokens`, `.input_tokens.cached`, `.output_tokens`, `.output_tokens.reasoning`, `.total_tokens`).
  - Prompts and responses are recorded **only if `send_default_pii=True` and `include_prompts=True`**.
  - `gen_ai.execute_tool` spans are created **only for Python callables in `config.tools`** run by AFC. With AFC disabled (section 1), you must add tool spans manually.
  - There is **no automatic `gen_ai.invoke_agent` span**.
  - If no transaction is active, each `generate_content` call starts its own transaction.
- **`init` options [DOC+SRC]:**
  - `traces_sample_rate=1.0` turns tracing on.
  - `send_default_pii=True` is needed for prompt and response content.
  - `stream_gen_ai_spans` defaults to `True` [SRC]: gen_ai spans are sent as separate `span` v2 envelope items, which I confirmed with a stub transport. Self-hosted Sentry may need `False`.
  - `trace_lifecycle="stream"` is an opt-in "span streaming" mode with a different API (`sentry_sdk.traces.*`). Do not mix the two modes.
- **Manual span conventions (official Python agent-tracing doc) [DOC]:**

| Span | `op` | `name` | Required data | Recommended / optional data |
|---|---|---|---|---|
| Agent run | `gen_ai.invoke_agent` | `invoke_agent {agent}` | `gen_ai.operation.name="invoke_agent"` | `gen_ai.agent.name`, `gen_ai.pipeline.name`, `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, usage tokens |
| LLM call | `gen_ai.chat` | `chat {model}` | `gen_ai.operation.name="chat"`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model` | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.reasoning.output_tokens`, `gen_ai.request.reasoning.level`, `gen_ai.input.messages`, `gen_ai.output.messages` |
| Tool | `gen_ai.execute_tool` | `execute_tool {tool}` | `gen_ai.operation.name="execute_tool"` | `gen_ai.tool.name`, `gen_ai.agent.name`, `gen_ai.tool.call.arguments` (JSON string), `gen_ai.tool.call.result` (JSON string), `gen_ai.tool.description`, `gen_ai.tool.type`. A failed tool should call `span.set_status("internal_error")`. |

  - **Deprecated keys:** `gen_ai.tool.input` is replaced by `gen_ai.tool.call.arguments`, and `gen_ai.tool.output` by `gen_ai.tool.call.result` [DOC+SRC]. Also deprecated: `gen_ai.response.text` and `gen_ai.request.messages`, replaced by the `*.messages` keys. `gen_ai.system` is replaced by `gen_ai.provider.name` [SRC docstring]. The auto-integration still writes the old keys.
  - Set attributes at span start so samplers can see them.
  - `@sentry_sdk.trace(op=, name=, template=SPANTEMPLATE.AI_AGENT|AI_TOOL|AI_CHAT)` exists [SRC] and works in transaction mode only [DOC].
  - `sentry_sdk.ai.utils.set_conversation_id(id)` groups traces into one conversation [SRC]. Do not derive that ID from a trace ID [DOC].
- **Trace ID [SRC]:** `tx.trace_id` from the transaction context manager, `sentry_sdk.get_current_span().trace_id`, or `sentry_sdk.get_traceparent()`. The last returns `"<32-hex trace_id>-<span_id>-<sampled>"`, so take the part before the first `-`.
- **One trace per background task [SRC, confirmed by running it]:**
  - In the default (non-streaming) mode, every `sentry_sdk.start_transaction(...)` that does not continue a trace **gets a fresh trace_id**, even when started inside another transaction or a request.
  - Wrap each task in `sentry_sdk.isolation_scope()` so tags and user data do not leak between concurrent tasks.
  - `continue_trace(headers)` is only for joining an *incoming* trace. Do not use it here.
  - In stream mode, the equivalent is `sentry_sdk.traces.new_trace()` followed by `sentry_sdk.traces.start_span(name=..., attributes={"sentry.op": ...})`.
- **AsyncioIntegration is not auto-enabled [SRC+DOC].** Add `AsyncioIntegration()` when `init()` runs inside a running loop, or call `sentry_sdk.integrations.asyncio.enable_asyncio_integration()` once a loop exists (e.g. in FastAPI lifespan). It captures unhandled task exceptions and adds task spans.
- **Trace URL:**
  - The documented Trace Explorer link is `https://sentry.io/orgredirect/organizations/<org_slug>/explore/traces/` [DOC].
  - A single trace is most likely `https://<org_slug>.sentry.io/explore/traces/trace/<trace_id>/`. That is UNVERIFIED, so confirm by opening one trace and copying the address bar.
  - The DSN contains only the numeric org ID (`o123…`), not the slug. Put the slug in an env var such as `SENTRY_ORG`.
- **Creating projects from the CLI (document only):**
  - Classic `sentry-cli` (`brew install getsentry/tools/sentry-cli`) documents releases, debug files, and similar commands. No project-create command was found (UNVERIFIED).
  - The newer `sentry` CLI from getsentry (cli.sentry.dev, install `curl https://cli.sentry.dev/install -fsS | bash`, log in with `sentry auth`, a browser flow) has `sentry project create [<org>/]<name>:<platform> -t <team>`, plus `sentry project view <org>/<project>`, whose output includes the DSN.
  - The REST alternative is `POST /api/0/teams/{org}/{team}/projects/` with scope `project:write`.

### Code

```python
import asyncio, json, os
import sentry_sdk
from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration

sentry_sdk.init(
    dsn=os.environ["SENTRY_DSN"],
    traces_sample_rate=1.0,
    send_default_pii=True,                       # needed to record prompts/responses on gen_ai spans
    integrations=[GoogleGenAIIntegration(include_prompts=True)],   # auto-enabled anyway; explicit for clarity
    environment=os.environ.get("APP_ENV", "dev"),
)
# In FastAPI lifespan startup (loop running):
#   from sentry_sdk.integrations.asyncio import enable_asyncio_integration; enable_asyncio_integration()

ORG = os.environ["SENTRY_ORG"]                   # org slug, not in the DSN

def trace_url(trace_id: str) -> str:
    return f"https://{ORG}.sentry.io/explore/traces/trace/{trace_id}/"   # path UNVERIFIED, see above

async def handle_flag(flag_id: str, user_text: str) -> str:
    """Run in asyncio.create_task(...); each call becomes its own transaction and trace."""
    with sentry_sdk.isolation_scope() as scope:
        scope.set_tag("flag_id", flag_id)
        with sentry_sdk.start_transaction(op="gen_ai.invoke_agent", name="invoke_agent call_desk_triage") as tx:
            tx.set_data("gen_ai.operation.name", "invoke_agent")
            tx.set_data("gen_ai.agent.name", "call_desk_triage")
            scope.set_tag("trace_url", trace_url(tx.trace_id))
            return await run_agent(user_text)    # generate_content calls appear as child gen_ai.chat spans

async def traced_tool(name: str, fn, args: dict):
    with sentry_sdk.start_span(op="gen_ai.execute_tool", name=f"execute_tool {name}") as span:
        span.set_data("gen_ai.operation.name", "execute_tool")
        span.set_data("gen_ai.tool.name", name)
        span.set_data("gen_ai.agent.name", "call_desk_triage")
        span.set_data("gen_ai.tool.call.arguments", json.dumps(args))
        try:
            result = await fn(**args)
        except Exception:
            span.set_status("internal_error")
            raise
        span.set_data("gen_ai.tool.call.result", json.dumps(result, default=str))
        return result
```

### Gotchas
- Without a surrounding transaction, the auto-integration makes **one transaction per LLM call**, which scatters the agent across many traces. Always open the `invoke_agent` transaction first.
- The `asyncio.create_task` call must happen *before* `start_transaction`. The transaction is started inside the task, as in `handle_flag`. A transaction started in the request handler would end when the response returns.
- Tool span arguments and results are strings. Use `json.dumps`. Avoid writing raw caller PII unless `send_default_pii` is intended.
- Call `sentry_sdk.flush()` before process exit in scripts, so the final transactions are sent.

### Sources
https://docs.sentry.io/platforms/python/integrations/google-genai/ · https://docs.sentry.io/platforms/python/agent-tracing/manual-instrumentation/ · https://docs.sentry.io/platforms/python/tracing/instrumentation/custom-instrumentation/ · https://docs.sentry.io/platforms/python/integrations/asyncio/ · https://docs.sentry.io/product/sentry-basics/tracing/trace-view/ · https://docs.sentry.io/cli/installation/ · https://cli.sentry.dev/commands/project/ · https://cli.sentry.dev/commands/trace/ · https://docs.sentry.io/api/projects/create-a-new-project/ · source: `.venv/.../sentry_sdk/integrations/google_genai/*`, `integrations/__init__.py`, `consts.py`, `scope.py`, `traces.py`, `tracing.py`
