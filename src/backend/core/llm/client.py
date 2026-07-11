"""Multi-provider LLM client.

Dispatches to either:
  - Google Gemini  (via google-genai SDK)  when LLM_PROVIDER=gemini  (default)
  - OpenAI / any OpenAI-compatible API     when LLM_PROVIDER=openai

OpenAI-compatible means: any service that implements the /chat/completions endpoint —
e.g. OpenAI, Ollama (local), Together AI, Groq, Mistral, LM Studio, Anyscale, etc.
Set OPENAI_BASE_URL to point at any compatible endpoint.

All paths return LLMTurnResult (validated via pydantic schema).
On any API or validation error, returns LLMTurnResult(response_type='error') —
never raises — so the loop can feed the error back without crashing.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import ValidationError

from .schema import LLMTurnResult
from ...models.dto import Message
from ...config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers if the model added them despite being asked not to."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = []
    in_block = False
    for line in lines:
        if line.startswith("```") and not in_block:
            in_block = True
            continue
        if line.startswith("```") and in_block:
            break
        if in_block:
            inner.append(line)
    return "\n".join(inner).strip()


def _repair_json(text: str) -> str:
    """Fix literal control characters (newline, tab, etc.) inside JSON strings.

    Some OpenAI-compatible models embed raw newlines/tabs in multi-line code
    values instead of escaping them as \\n / \\t, producing invalid JSON.
    This scans the string character by character and escapes any bare control
    characters that appear inside a JSON string literal.
    """
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            elif ord(ch) < 0x20:
                # other control characters
                result.append(f'\\u{ord(ch):04x}')
            else:
                result.append(ch)
        else:
            result.append(ch)
    return ''.join(result)


def _parse_llm_response(raw_text: str) -> LLMTurnResult:
    """Parse and validate raw LLM text into a LLMTurnResult.

    Returns a LLMTurnResult(response_type='error') on any parse / validation failure.
    """
    raw_text = _strip_markdown_fences(raw_text)

    try:
        data = json.loads(raw_text)
        if data.get("response_type") == "code" and "code" in data:
            data["code"] = _strip_markdown_fences(data["code"])
        return LLMTurnResult(**data)
    except json.JSONDecodeError:
        # Try repairing literal control characters (common with OpenAI models
        # that embed raw newlines in "code" field values)
        try:
            repaired = _repair_json(raw_text)
            data = json.loads(repaired)
            if data.get("response_type") == "code" and "code" in data:
                data["code"] = _strip_markdown_fences(data["code"])
            return LLMTurnResult(**data)
        except (json.JSONDecodeError, Exception) as e2:
            logger.warning("LLM returned non-JSON (repair failed): %s", raw_text[:300])
            return LLMTurnResult(
                response_type="error",
                message=(
                    f"Your last response was not valid JSON matching the contract. "
                    f"JSON parse error: {e2}. "
                    f"Raw response start: {raw_text[:300]}"
                ),
            )
    except ValidationError as e:
        logger.warning("LLM JSON failed schema validation: %s", e)
        return LLMTurnResult(
            response_type="error",
            message=(
                f"Your last response was valid JSON but did not match the required schema. "
                f"Validation error: {e}"
            ),
        )


def _build_messages(conversation: list[Message]) -> list[dict]:
    """Convert Message list to provider-agnostic role/content dicts."""
    return [
        {"role": msg.role, "content": msg.message}
        for msg in conversation
        if msg.role in ("user", "assistant")
    ]


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_client


def _send_gemini(conversation: list[Message], system_prompt: str) -> LLMTurnResult:
    """Call Gemini API via google-genai SDK."""
    try:
        from google.genai import types as genai_types
        client = _get_gemini_client()
        messages = _build_messages(conversation)

        # Convert to Gemini Content format
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append(
                genai_types.Content(
                    role=role,
                    parts=[genai_types.Part(text=msg["content"])],
                )
            )

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=4096,
                temperature=0.2,
            ),
        )

        raw_text = response.text or ""
        return _parse_llm_response(raw_text)

    except Exception as e:
        logger.error("Gemini API error: %s", e)
        return LLMTurnResult(
            response_type="error",
            message=f"Gemini API error: {e}",
        )


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible client
# ---------------------------------------------------------------------------

_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        kwargs = {"api_key": config.OPENAI_API_KEY}
        if config.OPENAI_BASE_URL:
            kwargs["base_url"] = config.OPENAI_BASE_URL
        _openai_client = OpenAI(**kwargs)
    return _openai_client


def _send_openai(conversation: list[Message], system_prompt: str) -> LLMTurnResult:
    """Call OpenAI or any OpenAI-compatible API (Together, Groq, Ollama, etc.)."""
    try:
        client = _get_openai_client()
        messages = [{"role": "system", "content": system_prompt}] + _build_messages(conversation)

        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            max_tokens=4096,
            temperature=0.2,
        )

        raw_text = response.choices[0].message.content or ""
        return _parse_llm_response(raw_text)

    except Exception as e:
        logger.error("OpenAI-compatible API error: %s", e)
        return LLMTurnResult(
            response_type="error",
            message=f"OpenAI-compatible API error: {e}",
        )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def send_turn(
    conversation: list[Message],
    system_prompt: str,
) -> LLMTurnResult:
    """Send the current conversation to the configured LLM provider.

    Dispatches to Gemini or OpenAI based on config.LLM_PROVIDER.
    Always returns a LLMTurnResult — never raises.

    Args:
        conversation: Full conversation history (user/assistant turns only).
        system_prompt: System-level prompt (built by prompts.py).

    Returns:
        Validated LLMTurnResult. status='error' on any failure.
    """
    provider = config.LLM_PROVIDER.lower()

    if provider == "openai":
        return _send_openai(conversation, system_prompt)
    elif provider == "gemini":
        return _send_gemini(conversation, system_prompt)
    else:
        logger.error("Unknown LLM_PROVIDER: %s", provider)
        return LLMTurnResult(
            response_type="error",
            message=(
                f"Unknown LLM_PROVIDER '{provider}'. "
                f"Set LLM_PROVIDER=gemini or LLM_PROVIDER=openai in .env."
            ),
        )
