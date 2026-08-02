"""Data transfer objects (request/response dataclasses and domain models)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ImageMetadata:
    """Metadata extracted from an uploaded image."""

    image_id: str
    original_filename: str
    shape: tuple[int, ...]          # (H, W, C) or (H, W) for grayscale
    dtype: str                      # "uint8", "uint16", "float32", ...
    channel_count: int
    size_bytes: int
    size_mb: float
    guessed_kind: str               # "rgb" | "rgba" | "grayscale" | "multichannel"
    value_range: tuple[float, float]
    path: str                       # full-res path in volumes/uploads
    preview_path: str               # downsampled preview path

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "original_filename": self.original_filename,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "channel_count": self.channel_count,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_mb, 3),
            "guessed_kind": self.guessed_kind,
            "value_range": list(self.value_range),
            "path": self.path,
            "preview_path": self.preview_path,
        }


@dataclass
class Message:
    """A single turn in an LLM conversation."""

    role: str                     # "user" | "assistant"
    message: str                  # human-readable text — always present
    turn_index: int
    response_type: str | None = None  # "chat" | "code"; None for user-authored turns
    code: str | None = None           # present when response_type == "code" or illustrative
    was_executed: bool = False        # true only if this message's code actually ran
    execution_result: ExecutionResult | None = None # populated only if was_executed
    internal: bool = False            # loop-internal turn (execution feedback / contract-violation
                                      # feedback) — part of the LLM context, hidden in the chat UI
    pipeline: dict[str, Any] | None = None   # graph JSON when response_type == "pipeline" (v3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "response_type": self.response_type,
            "message": self.message,
            "code": self.code,
            "was_executed": self.was_executed,
            "execution_result": self.execution_result.to_dict() if self.execution_result else None,
            "turn_index": self.turn_index,
            "internal": self.internal,
            "pipeline": self.pipeline,
        }


@dataclass
class GeneratedImage:
    """An output image produced by the sandbox (from LLM or manual run)."""

    image_id: str
    session_id: str
    description: str
    path: str                   # full-res output path
    preview_path: str
    code: str                   # exact code that produced this image
    source_turn_index: int | None # links back to the Message that produced it
    produced_at: datetime
    source_iteration: int       # loop attempt (llm) or run number (manual)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "session_id": self.session_id,
            "description": self.description,
            "path": self.path,
            "preview_path": self.preview_path,
            "code": self.code,
            "source_turn_index": self.source_turn_index,
            "produced_at": self.produced_at.isoformat(),
            "source_iteration": self.source_iteration,
        }


@dataclass
class Session:
    """A unified session — LLM chat AND self-programming editor, toggled in the
    UI, sharing the same images/outputs/history. Replaces the old mode-locked
    Window (see DECISIONS D-018)."""

    session_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    title: str = "Untitled"
    image_ids: list[str] = field(default_factory=list)  # all attached images
    llm_conversation: list[Message] = field(default_factory=list)
    current_code: str | None = None         # manual editor content
    outputs: list[GeneratedImage] = field(default_factory=list)
    status: str = "idle"                    # "idle" | "running" | "error"
    _run_count: int = 0                     # manual-run counter

    # Sharing is NOT a field on Session — share links live in the separate
    # `share_links` table (token, session_id, reusable, used_count), since a
    # session can have multiple links over time and sharing now means
    # deep-clone-on-open, not a flag on the session itself (see D-019).

    @property
    def image_id(self) -> str | None:
        """Primary (first-attached) image — convenience accessor."""
        return self.image_ids[0] if self.image_ids else None

    def next_run_number(self) -> int:
        self._run_count += 1
        return self._run_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "image_id": self.image_id,
            "image_ids": self.image_ids,
            "status": self.status,
            "output_count": len(self.outputs),
        }


@dataclass
class ExecutionResult:
    """Result of a sandboxed code execution."""

    stdout: str
    stderr: str
    traceback: str | None
    time_taken_seconds: float
    file_exists: bool
    output_path: str | None
    timed_out: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "traceback": self.traceback,
            "time_taken_seconds": round(self.time_taken_seconds, 3),
            "file_exists": self.file_exists,
            "output_path": self.output_path,
            "timed_out": self.timed_out,
        }
