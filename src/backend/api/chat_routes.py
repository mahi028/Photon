"""LLM chat routes: POST /api/windows/<id>/message and GET /api/windows/<id>/stream.

Architecture:
- POST /message triggers the generate→execute→fix loop in a background thread.
- GET /stream is an SSE endpoint the browser subscribes to for progress events.
- Shared state per window_id stored in _sse_queues (thread-safe Queue).

TODO(spec): For production, replace in-process threading with a task queue
(Celery/RQ) and SSE with a proper pub/sub channel.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Generator, Iterator

from flask import Blueprint, Response, request, jsonify, stream_with_context

from ..config import config
from ..core.llm.client import send_turn
from ..core.llm.prompts import build_system_prompt, build_execution_feedback_message
from ..core.llm.schema import LLMTurnResult
from ..core.sandbox.subprocess_executor import SubprocessExecutor
from ..core.image.thumbnail import generate_thumbnail
from ..core.session.manager import get_manager
from ..models.dto import Message, GeneratedImage
from .upload_routes import get_image_registry

chat_bp = Blueprint("chat", __name__)
_executor = SubprocessExecutor()

# Per-window SSE event queues: window_id -> Queue[dict | None]
# None is the sentinel that signals the stream is finished.
_sse_queues: dict[str, queue.Queue] = {}
_sse_lock = threading.Lock()

# Per-window stop flags: POST /stop sets the event; the loop checks it at
# safe boundaries (before each LLM call, before each sandbox execution).
_stop_events: dict[str, threading.Event] = {}


def _get_stop_event(window_id: str) -> threading.Event:
    with _sse_lock:
        if window_id not in _stop_events:
            _stop_events[window_id] = threading.Event()
        return _stop_events[window_id]


def _get_or_create_queue(window_id: str) -> queue.Queue:
    with _sse_lock:
        if window_id not in _sse_queues:
            _sse_queues[window_id] = queue.Queue()
        return _sse_queues[window_id]


def _push_event(window_id: str, event: dict) -> None:
    q = _get_or_create_queue(window_id)
    q.put(event)


def _finish_stream(window_id: str) -> None:
    q = _get_or_create_queue(window_id)
    q.put(None)  # sentinel


# ---------------------------------------------------------------------------
# The LLM loop — runs in a background thread
# ---------------------------------------------------------------------------

def _run_llm_loop(window_id: str, user_prompt: str) -> None:
    """Core generate→execute→fix loop. Runs in a daemon thread."""
    manager = get_manager()
    window = manager.get_window(window_id)
    if not window:
        _push_event(window_id, {"event": "error", "message": "Window not found"})
        _finish_stream(window_id)
        return

    image_registry = get_image_registry()
    metadata = image_registry.get(window.image_id)
    if not metadata:
        _push_event(window_id, {"event": "error", "message": "Attached image not found"})
        _finish_stream(window_id)
        return

    manager.set_status(window_id, "running")
    system_prompt = build_system_prompt(metadata)

    # Append the user message to the conversation (write-through to SQLite — Section 5.3)
    manager.add_message(
        window_id,
        Message(role="user", message=user_prompt, turn_index=len(window.llm_conversation)),
    )

    max_iter = config.MAX_LOOP_ITERATIONS
    output_dir = config.OUTPUTS_DIR / window_id
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = metadata.path

    last_successful_output: str | None = None
    last_error: str | None = None
    stop_event = _get_stop_event(window_id)
    stopped = False

    for attempt in range(1, max_iter + 1):
        if stop_event.is_set():
            stopped = True
            break

        _push_event(window_id, {
            "event": "attempt",
            "attempt": attempt,
            "max": max_iter,
            "message": f"Calling LLM (attempt {attempt}/{max_iter})...",
        })

        # --- Call LLM ---
        llm_result = send_turn(
            conversation=window.llm_conversation,
            system_prompt=system_prompt,
        )

        # Build the assistant message. For "code" replies it is persisted AFTER
        # execution, so the stored row carries was_executed/execution_result;
        # chat/error replies are persisted immediately (write-through — Section 5.3).
        assistant_msg = Message(
            role="assistant",
            response_type=llm_result.response_type,
            message=llm_result.message,
            code=llm_result.code,
            turn_index=len(window.llm_conversation),
        )

        # Push message to UI
        _push_event(window_id, {
            "event": "llm_message",
            "attempt": attempt,
            "response_type": llm_result.response_type,
            "message": llm_result.message,
            "code": llm_result.code,
        })

        if llm_result.response_type == "error":
            manager.add_message(window_id, assistant_msg)
            # Feed the error back as a user turn for the LLM to correct itself
            feedback = f"Error: {llm_result.message}\nPlease fix your JSON response."
            manager.add_message(
                window_id,
                Message(role="user", message=feedback,
                        turn_index=len(window.llm_conversation), internal=True),
            )
            last_error = llm_result.message
            continue

        if llm_result.response_type == "chat":
            manager.add_message(window_id, assistant_msg)
            _push_event(window_id, {
                "event": "chat_turn",
                "message": llm_result.message,
            })
            break

        # response_type == "code" — execute the code
        # Last stop check before committing to a (up to timeout-long) execution.
        if stop_event.is_set():
            manager.add_message(window_id, assistant_msg)  # persisted, never executed
            stopped = True
            break

        _push_event(window_id, {
            "event": "executing",
            "attempt": attempt,
            "message": f"Running code in sandbox...",
        })

        exec_result = _executor.execute(
            code=llm_result.code or "",
            input_path=input_path,
            output_dir=str(output_dir),
            timeout=config.EXECUTION_TIMEOUT_SECONDS,
        )

        _push_event(window_id, {
            "event": "exec_result",
            "attempt": attempt,
            "file_exists": exec_result.file_exists,
            "timed_out": exec_result.timed_out,
            "stderr": exec_result.stderr[:500] if exec_result.stderr else "",
            "traceback": exec_result.traceback[:500] if exec_result.traceback else None,
            "time_taken": exec_result.time_taken_seconds,
        })

        # Attach execution result, then persist the assistant turn (so the
        # stored row includes was_executed/execution_result — Section 5.3)
        assistant_msg.was_executed = True
        assistant_msg.execution_result = exec_result
        manager.add_message(window_id, assistant_msg)

        if exec_result.file_exists:
            # Success — stop immediately. No more LLM calls needed.
            last_successful_output = exec_result.output_path
            break

        # Only append error feedback to conversation when execution actually failed
        feedback_msg = build_execution_feedback_message(exec_result, attempt, max_iter)
        manager.add_message(
            window_id,
            Message(
                role="user",
                message=feedback_msg,
                turn_index=len(window.llm_conversation),
                internal=True,
            ),
        )
        last_error = exec_result.stderr or exec_result.traceback

    else:
        # Loop exhausted without "done"
        _push_event(window_id, {
            "event": "error",
            "message": f"Gave up after {max_iter} attempts.",
            "last_error": last_error or "Unknown error",
        })

    if stopped:
        # User aborted the loop — record a visible chat turn and end the stream.
        manager.add_message(
            window_id,
            Message(
                role="assistant",
                response_type="chat",
                message="⏹ Stopped by user.",
                turn_index=len(window.llm_conversation),
            ),
        )
        _push_event(window_id, {"event": "stopped", "message": "⏹ Stopped by user."})

    # Extract the winning code
    code = None
    for msg in reversed(window.llm_conversation):
        if msg.role == "assistant" and getattr(msg, "response_type", None) == "code" and msg.code:
            code = msg.code
            break

    # Run on ALL images in the batch (primary already ran above; re-run to get output,
    # then run remaining images). For simplicity we just run all image_ids now.
    if last_successful_output and code and not stopped:
        for slot_idx, img_id in enumerate(window.image_ids):
            if stop_event.is_set():
                _push_event(window_id, {"event": "stopped", "message": "⏹ Stopped during batch run."})
                break
            img_meta = image_registry.get(img_id)
            if not img_meta:
                continue
            img_input_path = img_meta.path
            img_output_dir = config.OUTPUTS_DIR / window_id
            img_output_dir.mkdir(parents=True, exist_ok=True)

            # Primary image output is already in last_successful_output
            if img_id == window.image_id and slot_idx == 0:
                slot_output_path = Path(last_successful_output)
            else:
                # Execute on this image
                _push_event(window_id, {
                    "event": "batch_executing",
                    "image_slot": slot_idx,
                    "image_id": img_id,
                    "message": f"Running on image {slot_idx + 1}/{len(window.image_ids)}...",
                })
                slot_result = _executor.execute(
                    code=code,
                    input_path=img_input_path,
                    output_dir=str(img_output_dir),
                    timeout=config.EXECUTION_TIMEOUT_SECONDS,
                )
                if not slot_result.file_exists or not slot_result.output_path:
                    _push_event(window_id, {
                        "event": "batch_error",
                        "image_slot": slot_idx,
                        "image_id": img_id,
                        "message": slot_result.stderr or slot_result.traceback or "Unknown error",
                    })
                    continue
                slot_output_path = Path(slot_result.output_path)

            # Generate preview
            preview_path = slot_output_path.parent / f"{slot_output_path.stem}_preview.jpg"
            try:
                generate_thumbnail(slot_output_path, preview_path)
            except Exception:
                pass

            # Write .py sidecar
            try:
                slot_output_path.with_suffix(".py").write_text(code, encoding="utf-8")
            except Exception:
                pass

            output_image_id = uuid.uuid4().hex
            description = f"llm_output_slot{slot_idx}_{len(window.outputs) + 1}"
            gen_image = GeneratedImage(
                image_id=output_image_id,
                window_id=window_id,
                description=description,
                path=str(slot_output_path),
                preview_path=str(preview_path) if preview_path.exists() else "",
                code=code,
                source_turn_index=None,
                produced_at=datetime.utcnow(),
                source_iteration=attempt,
            )
            manager.add_output(window_id, gen_image)

            prev_rel = preview_path.relative_to(config.OUTPUTS_DIR)
            preview_url = f"/outputs/{prev_rel.as_posix()}"

            _push_event(window_id, {
                "event": "output_saved",
                "image_slot": slot_idx,
                "source_image_id": img_id,
                "image_id": output_image_id,
                "preview_url": preview_url,
                "description": description,
            })

    manager.set_status(window_id, "idle")
    _finish_stream(window_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@chat_bp.route("/windows/<window_id>/message", methods=["POST"])
def send_message(window_id: str):
    """Kick off the LLM generate→execute→fix loop."""
    manager = get_manager()
    window = manager.get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404
    if window.mode != "llm":
        return jsonify({"error": "This endpoint is for LLM-mode windows only"}), 400
    if window.status == "running":
        return jsonify({"error": "Window is already running"}), 409

    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    # Reset any old queue and stop flag for this window
    with _sse_lock:
        _sse_queues[window_id] = queue.Queue()
    _get_stop_event(window_id).clear()

    thread = threading.Thread(
        target=_run_llm_loop,
        args=(window_id, prompt),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "window_id": window_id}), 202


@chat_bp.route("/windows/<window_id>/stop", methods=["POST"])
def stop_loop(window_id: str):
    """Request the in-flight LLM loop for this window to stop.

    The loop honors the flag at its next safe boundary (before the next LLM
    call or sandbox execution) — an execution already in flight runs to
    completion/timeout. Idempotent: stopping an idle window is a no-op.
    """
    manager = get_manager()
    window = manager.get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404
    if window.mode != "llm":
        return jsonify({"error": "This endpoint is for LLM-mode windows only"}), 400

    _get_stop_event(window_id).set()
    return jsonify({"status": "stop_requested", "window_id": window_id}), 202


@chat_bp.route("/windows/<window_id>/stream", methods=["GET"])
def stream_events(window_id: str):
    """SSE stream of loop progress for an LLM window."""
    manager = get_manager()
    window = manager.get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404
    if window.mode != "llm":
        return jsonify({"error": "SSE stream is for LLM-mode windows only"}), 400

    q = _get_or_create_queue(window_id)

    def event_generator() -> Iterator[str]:
        while True:
            try:
                event = q.get(timeout=15)
            except queue.Empty:
                # Send a keepalive comment
                yield ": keepalive\n\n"
                continue

            if event is None:
                # Sentinel — stream complete
                yield "data: {\"event\": \"stream_end\"}\n\n"
                return

            yield f"data: {json.dumps(event)}\n\n"

    return Response(
        stream_with_context(event_generator()),  # type: ignore
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
