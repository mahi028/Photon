"""Pydantic models for LLM I/O validation.

The LLM must return strict JSON matching LLMTurnResult.
On validation failure, callers receive a LLMTurnResult with status='error'
so the loop can feed a synthetic error back without crashing.
"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, field_validator


class LLMTurnResult(BaseModel):
    """Parsed and validated response from the LLM for a single turn."""

    response_type: Literal["chat", "code", "error"]
    message: str
    code: Optional[str] = None

    @field_validator("code")
    @classmethod
    def code_required_when_code(cls, v: Optional[str], info) -> Optional[str]:
        # Access response_type via info.data (pydantic v2)
        response_type = info.data.get("response_type")
        if response_type == "code" and not v:
            raise ValueError("'code' must be provided when response_type is 'code'")
        return v
