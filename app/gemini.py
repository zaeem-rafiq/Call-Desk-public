"""Gemini: chunk transcription + claim extraction, and the fact-check agent.

Implemented in M2/M4 after checking current google-genai docs (docs/sdk_notes.md).
"""

from __future__ import annotations


async def transcribe_and_extract(*_args, **_kwargs):
    raise NotImplementedError("Gemini transcription lands in M2")


async def agent_fact_check(*_args, **_kwargs):
    raise NotImplementedError("Gemini agent lands in M4")
