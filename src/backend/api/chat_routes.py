"""LLM chat routes: POST /api/sessions/<id>/message and GET /api/sessions/<id>/stream.

Architecture:
- POST /message triggers the generate→execute→fix loop in a background thread.
- GET /stream is an SSE endpoint the browser subscribes to for progress events.
- Shared state per session_id stored in _sse_queues (thread-safe Queue).

A session no longer has a locked "mode" (see DECISIONS D-018) — any session can
receive an LLM message and/or a manual run-code call; both write into the same
llm_conversation/current_code/outputs.

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
from ..core.auth.middleware import login_required, get_current_user_id
from ..core.llm.client import send_turn
from ..core.llm.prompts import (
    build_execution_feedback_message,
    build_pipeline_feedback_message,
    build_pipeline_result_message,
    build_system_prompt,
)
from ..core.llm.schema import LLMTurnResult
from ..core.pipeline import runner as pipeline_runner
from ..core.session import store
from ..core.sandbox.backend import get_executor
from ..core.image.thumbnail import generate_thumbnail
from ..core.session.manager import get_manager
from ..models.dto import Message, GeneratedImage
from .upload_routes import get_image_registry

chat_bp = Blueprint("chat", __name__)

# Per-session SSE event queues: session_id -> Queue[dict | None]
# None is the sentinel that signals the stream is finished.
_sse_queues: dict[str, queue.Queue] = {}
_sse_lock = threading.Lock()

# Per-session stop flags: POST /stop sets the event; the loop checks it at
# safe boundaries (before each LLM call, before each sandbox execution).
_stop_events: dict[str, threading.Event] = {}


def _get_stop_event(session_id: str) -> threading.Event:
    with _sse_lock:
        if session_id not in _stop_events:
            _stop_events[session_id] = threading.Event()
        return _stop_events[session_id]


def _get_or_create_queue(session_id: str) -> queue.Queue:
    with _sse_lock:
        if session_id not in _sse_queues:
            _sse_queues[session_id] = queue.Queue()
        return _sse_queues[session_id]


def _push_event(session_id: str, event: dict) -> None:
    q = _get_or_create_queue(session_id)
    q.put(event)


def _finish_stream(session_id: str) -> None:
    q = _get_or_create_queue(session_id)
    q.put(None)  # sentinel


# ---------------------------------------------------------------------------
# The LLM loop — runs in a background thread
# ---------------------------------------------------------------------------

def _persist_inline_custom_nodes(graph: dict, user_id: str) -> tuple[dict, list[dict]]:
    """Save any inline `custom` nodes the LLM wrote, rewriting them to
    `custom_node_id` references.

    This is what makes "retrieve if it exists, generate only when missing"
    work: saved nodes appear in the prompt catalog on later turns, so the LLM
    references them by id instead of rewriting the code.

    Raises ValueError if a node's code fails the sandbox AST check — the caller
    turns that into a feedback turn rather than crashing the loop.
    """
    from ..core.sandbox.subprocess_executor import ASTSecurityError, _check_ast_safety

    saved: list[dict] = []
    nodes = []
    for node in (graph.get("nodes") or []):
        spec = node.get("custom")
        if not spec:
            nodes.append(node)
            continue

        name = (spec.get("name") or "").strip()
        code = spec.get("code") or ""
        ports = spec.get("ports") or {}
        if not name or not code.strip():
            raise ValueError(f"Custom node in node '{node.get('id')}' is missing name or code.")
        if not ports.get("inputs") or not ports.get("outputs"):
            raise ValueError(f"Custom node '{name}' must declare ports.inputs and ports.outputs.")
        if "def node(" not in code:
            raise ValueError(f"Custom node '{name}' must define node(inputs, params).")
        try:
            _check_ast_safety(code, strict_io=True)
        except (ASTSecurityError, SyntaxError) as e:
            raise ValueError(f"Custom node '{name}' rejected by sandbox policy: {e}")

        existing = store.find_custom_node_by_name(user_id, name)
        node_id = existing["custom_node_id"] if existing else uuid.uuid4().hex
        store.save_custom_node(node_id, user_id, name, code, ports,
                               spec.get("params") or [], (spec.get("doc") or "").strip(),
                               datetime.utcnow().isoformat())
        saved.append({"custom_node_id": node_id, "name": name})

        rewritten = {k: v for k, v in node.items() if k != "custom"}
        rewritten["custom_node_id"] = node_id
        nodes.append(rewritten)

    return {**graph, "nodes": nodes}, saved


def _run_llm_loop(session_id: str, user_prompt: str) -> None:
    """Thread entry point: the loop body, plus releasing the session and
    closing the stream if it raises (otherwise the session stays 'running'
    and the browser waits on a stream that never ends)."""
    claimed: list[bool] = []
    try:
        _llm_loop_body(session_id, user_prompt, claimed)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("LLM loop crashed for session %s", session_id)
        _push_event(session_id, {"event": "error", "message": f"Internal error: {e}"})
        if claimed:
            get_manager().set_status(session_id, "idle")
        _finish_stream(session_id)


def _llm_loop_body(session_id: str, user_prompt: str, claimed: list) -> None:
    """Core generate→execute→fix loop."""
    manager = get_manager()
    sess = manager.get_session(session_id)
    if not sess:
        _push_event(session_id, {"event": "error", "message": "Session not found"})
        _finish_stream(session_id)
        return

    image_registry = get_image_registry()
    metadata = image_registry.get(sess.image_id) if sess.image_id else None
    if not metadata:
        _push_event(session_id, {"event": "error", "message": "Attached image not found"})
        _finish_stream(session_id)
        return

    if not manager.try_start_run(session_id):
        _push_event(session_id, {"event": "error", "message": "Session is already running"})
        _finish_stream(session_id)
        return
    claimed.append(True)
    # The user's saved custom nodes go into the catalog so the LLM reuses them
    # (by custom_node_id) instead of regenerating equivalent code (v3 P2).
    custom_nodes = store.list_custom_nodes(sess.user_id)
    from ..core.pipeline import publishing
    system_prompt = build_system_prompt(metadata, custom_nodes=custom_nodes,
                                        published=publishing.list_public(sess.user_id))

    # Append the user message to the conversation (write-through to SQLite — Section 5.3)
    manager.add_message(
        session_id,
        Message(role="user", message=user_prompt, turn_index=len(sess.llm_conversation)),
    )

    max_iter = config.MAX_LOOP_ITERATIONS
    # Unique dir per run + per slot: generated code often saves a fixed filename
    # (e.g. "output.png"), so a shared dir makes slots/turns overwrite each other.
    exec_run_id = uuid.uuid4().hex[:8]
    output_dir = config.OUTPUTS_DIR / session_id / exec_run_id / "slot0"
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = metadata.path

    last_successful_output: str | None = None
    last_error: str | None = None
    stop_event = _get_stop_event(session_id)
    stopped = False

    for attempt in range(1, max_iter + 1):
        if stop_event.is_set():
            stopped = True
            break

        _push_event(session_id, {
            "event": "attempt",
            "attempt": attempt,
            "max": max_iter,
            "message": f"Calling LLM (attempt {attempt}/{max_iter})...",
        })

        # --- Call LLM ---
        llm_result = send_turn(
            conversation=sess.llm_conversation,
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
            pipeline=llm_result.pipeline,
            turn_index=len(sess.llm_conversation),
        )

        # Push message to UI
        _push_event(session_id, {
            "event": "llm_message",
            "attempt": attempt,
            "response_type": llm_result.response_type,
            "message": llm_result.message,
            "code": llm_result.code,
            "pipeline": llm_result.pipeline,
        })

        if llm_result.response_type == "error":
            manager.add_message(session_id, assistant_msg)
            # Feed the error back as a user turn for the LLM to correct itself
            feedback = f"Error: {llm_result.message}\nPlease fix your JSON response."
            manager.add_message(
                session_id,
                Message(role="user", message=feedback,
                        turn_index=len(sess.llm_conversation), internal=True),
            )
            last_error = llm_result.message
            continue

        if llm_result.response_type == "chat":
            manager.add_message(session_id, assistant_msg)
            _push_event(session_id, {
                "event": "chat_turn",
                "message": llm_result.message,
            })
            break

        if llm_result.response_type == "pipeline":
            # Compose from the registry instead of generating code (v3 P2).
            if stop_event.is_set():
                manager.add_message(session_id, assistant_msg)
                stopped = True
                break

            graph = llm_result.pipeline or {}
            run_customs = run_provenance = None
            if graph.get("published_id"):
                # A shared pipeline: run its frozen snapshot (D-035), no edits.
                from ..core.pipeline import publishing
                try:
                    graph, run_customs, _, run_provenance = publishing.resolve_for_run(
                        sess.user_id, str(graph["published_id"]),
                        int(graph["version"]) if graph.get("version") else None)
                except (publishing.PublishError, ValueError, TypeError) as e:
                    manager.add_message(session_id, assistant_msg)
                    manager.add_message(session_id, Message(
                        role="user", internal=True, turn_index=len(sess.llm_conversation),
                        message=f"## Shared pipeline not available\n\n{e}\n\n"
                                "Use a published_id from the Shared Pipelines list, or compose "
                                "your own pipeline from catalog ops."))
                    last_error = str(e)
                    continue
            try:
                graph, saved_customs = _persist_inline_custom_nodes(graph, sess.user_id)
            except ValueError as e:
                manager.add_message(session_id, assistant_msg)
                manager.add_message(session_id, Message(
                    role="user", internal=True, turn_index=len(sess.llm_conversation),
                    message=f"## Custom node rejected\n\n{e}\n\n"
                            "Fix the node code (it must define node(inputs, params) and obey "
                            "the library whitelist) or use catalog ops only."))
                last_error = str(e)
                continue

            customs = run_customs if run_customs is not None else {
                cn["custom_node_id"]: cn for cn in store.list_custom_nodes(sess.user_id)}
            issues = pipeline_runner.validate(graph, custom_nodes=customs)
            if issues:
                issue_dicts = [i.to_dict() for i in issues]
                assistant_msg.pipeline = graph
                manager.add_message(session_id, assistant_msg)
                _push_event(session_id, {"event": "pipeline_invalid", "attempt": attempt,
                                         "issues": issue_dicts})
                manager.add_message(session_id, Message(
                    role="user", internal=True, turn_index=len(sess.llm_conversation),
                    message=build_pipeline_feedback_message(issue_dicts, attempt, max_iter)))
                last_error = "; ".join(i["message"] for i in issue_dicts[:3])
                continue

            assistant_msg.pipeline = graph
            assistant_msg.was_executed = True
            manager.add_message(session_id, assistant_msg)
            turn_index = assistant_msg.turn_index

            from .pipeline_routes import run_pipeline_for_session
            run_result = run_pipeline_for_session(
                session_id, graph, title="llm", user_id=sess.user_id,
                source_turn_index=turn_index, manage_status=False,
                custom_nodes=run_customs, provenance=run_provenance)

            if run_result.get("error"):
                last_error = run_result["error"]
                manager.add_message(session_id, Message(
                    role="user", internal=True, turn_index=len(sess.llm_conversation),
                    message=build_pipeline_result_message(run_result, attempt, max_iter)))
                continue

            if saved_customs:
                _push_event(session_id, {"event": "custom_nodes_saved",
                                         "nodes": saved_customs})
            # Success: pipeline ran and produced outputs — done for this turn.
            break

        # response_type == "code" — execute the code
        # Last stop check before committing to a (up to timeout-long) execution.
        if stop_event.is_set():
            manager.add_message(session_id, assistant_msg)  # persisted, never executed
            stopped = True
            break

        _push_event(session_id, {
            "event": "executing",
            "attempt": attempt,
            "message": f"Running code in sandbox...",
        })

        exec_result = get_executor().execute(
            code=llm_result.code or "",
            input_path=input_path,
            output_dir=str(output_dir),
            timeout=config.EXECUTION_TIMEOUT_SECONDS,
        )

        _push_event(session_id, {
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
        manager.add_message(session_id, assistant_msg)

        if exec_result.file_exists:
            # Success — stop immediately. No more LLM calls needed.
            last_successful_output = exec_result.output_path
            break

        # Only append error feedback to conversation when execution actually failed
        feedback_msg = build_execution_feedback_message(exec_result, attempt, max_iter)
        manager.add_message(
            session_id,
            Message(
                role="user",
                message=feedback_msg,
                turn_index=len(sess.llm_conversation),
                internal=True,
            ),
        )
        last_error = exec_result.stderr or exec_result.traceback

    else:
        # Loop exhausted without "done"
        _push_event(session_id, {
            "event": "error",
            "message": f"Gave up after {max_iter} attempts.",
            "last_error": last_error or "Unknown error",
        })

    if stopped:
        # User aborted the loop — record a visible chat turn and end the stream.
        manager.add_message(
            session_id,
            Message(
                role="assistant",
                response_type="chat",
                message="⏹ Stopped by user.",
                turn_index=len(sess.llm_conversation),
            ),
        )
        _push_event(session_id, {"event": "stopped", "message": "⏹ Stopped by user."})

    # Extract the winning code (and its turn, so outputs link back to the
    # message that produced them — powers the chat card's "Preview Output")
    code = None
    winning_turn_index = None
    for msg in reversed(sess.llm_conversation):
        if msg.role == "assistant" and getattr(msg, "response_type", None) == "code" and msg.code:
            code = msg.code
            winning_turn_index = msg.turn_index
            break

    # Run on ALL images in the batch (primary already ran above; re-run to get output,
    # then run remaining images). For simplicity we just run all image_ids now.
    if last_successful_output and code and not stopped:
        for slot_idx, img_id in enumerate(sess.image_ids):
            if stop_event.is_set():
                _push_event(session_id, {"event": "stopped", "message": "⏹ Stopped during batch run."})
                break
            img_meta = image_registry.get(img_id)
            if not img_meta:
                continue
            img_input_path = img_meta.path
            img_output_dir = config.OUTPUTS_DIR / session_id / exec_run_id / f"slot{slot_idx}"
            img_output_dir.mkdir(parents=True, exist_ok=True)

            # Primary image output is already in last_successful_output
            if img_id == sess.image_id and slot_idx == 0:
                slot_output_path = Path(last_successful_output)
            else:
                # Execute on this image
                _push_event(session_id, {
                    "event": "batch_executing",
                    "image_slot": slot_idx,
                    "image_id": img_id,
                    "message": f"Running on image {slot_idx + 1}/{len(sess.image_ids)}...",
                })
                slot_result = get_executor().execute(
                    code=code,
                    input_path=img_input_path,
                    output_dir=str(img_output_dir),
                    timeout=config.EXECUTION_TIMEOUT_SECONDS,
                )
                if not slot_result.file_exists or not slot_result.output_path:
                    _push_event(session_id, {
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
            description = f"llm_output_slot{slot_idx}_{len(sess.outputs) + 1}"
            gen_image = GeneratedImage(
                image_id=output_image_id,
                session_id=session_id,
                description=description,
                path=str(slot_output_path),
                preview_path=str(preview_path) if preview_path.exists() else "",
                code=code,
                source_turn_index=winning_turn_index,
                produced_at=datetime.utcnow(),
                source_iteration=attempt,
            )
            manager.add_output(session_id, gen_image)

            prev_rel = preview_path.relative_to(config.OUTPUTS_DIR)
            preview_url = f"/outputs/{prev_rel.as_posix()}"

            _push_event(session_id, {
                "event": "output_saved",
                "image_slot": slot_idx,
                "source_image_id": img_id,
                "image_id": output_image_id,
                "preview_url": preview_url,
                "description": description,
            })

    manager.set_status(session_id, "idle")
    _finish_stream(session_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@chat_bp.route("/sessions/<session_id>/message", methods=["POST"])
@login_required
def send_message(session_id: str):
    """Kick off the LLM generate→execute→fix loop. Owner only."""
    manager = get_manager()
    sess = manager.get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    if sess.status == "running":
        return jsonify({"error": "Session is already running"}), 409
    if not sess.image_id:
        return jsonify({"error": "Session has no attached image"}), 400

    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    # Reset any old queue and stop flag for this session
    with _sse_lock:
        _sse_queues[session_id] = queue.Queue()
    _get_stop_event(session_id).clear()

    thread = threading.Thread(
        target=_run_llm_loop,
        args=(session_id, prompt),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "started", "session_id": session_id}), 202


@chat_bp.route("/sessions/<session_id>/stop", methods=["POST"])
@login_required
def stop_loop(session_id: str):
    """Request the in-flight LLM loop for this session to stop. Owner only.

    The loop honors the flag at its next safe boundary (before the next LLM
    call or sandbox execution) — an execution already in flight runs to
    completion/timeout. Idempotent: stopping an idle session is a no-op.
    """
    manager = get_manager()
    sess = manager.get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    _get_stop_event(session_id).set()
    return jsonify({"status": "stop_requested", "session_id": session_id}), 202


@chat_bp.route("/sessions/<session_id>/stream", methods=["GET"])
@login_required
def stream_events(session_id: str):
    """SSE stream of loop progress for a session. Owner only."""
    manager = get_manager()
    sess = manager.get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    q = _get_or_create_queue(session_id)

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
