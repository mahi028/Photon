"""Pydantic models for LLM I/O validation.

The LLM must return strict JSON matching LLMTurnResult.
On validation failure, callers receive a LLMTurnResult with status='error'
so the loop can feed a synthetic error back without crashing.
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, field_validator


class LLMTurnResult(BaseModel):
    """Parsed and validated response from the LLM for a single turn."""

    response_type: Literal["chat", "code", "pipeline", "error"]
    message: str
    code: Optional[str] = None
    # Raw graph dict: {"nodes": [...], "edges": [...]}. Kept as a plain dict so
    # runner.validate() stays the single source of truth for graph correctness
    # (pydantic would duplicate — and could disagree with — that logic).
    pipeline: Optional[dict[str, Any]] = None

    @field_validator("code")
    @classmethod
    def code_required_when_code(cls, v: Optional[str], info) -> Optional[str]:
        # Access response_type via info.data (pydantic v2)
        response_type = info.data.get("response_type")
        if response_type == "code" and not v:
            raise ValueError("'code' must be provided when response_type is 'code'")
        return v

    @field_validator("pipeline")
    @classmethod
    def pipeline_required_when_pipeline(cls, v, info):
        response_type = info.data.get("response_type")
        if response_type == "pipeline":
            if not v:
                raise ValueError("'pipeline' must be provided when response_type is 'pipeline'")
            if not isinstance(v, dict) or not v.get("nodes"):
                raise ValueError("'pipeline' must be an object with a non-empty 'nodes' list")
        return v
