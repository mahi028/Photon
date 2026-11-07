"""Pipeline routes: op catalog, pipeline CRUD, custom nodes, and execution.

All routes are @login_required and owner-scoped (404, not 403, for anything
the caller doesn't own — same convention as session routes, D-018). Presets
(user_id NULL) are readable by everyone but never mutable: clone to edit.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..config import config
from ..core.auth.middleware import get_current_user_id, login_required
from ..core.image.thumbnail import generate_thumbnail
from ..core.pipeline import publishing, registry, runner
from ..core.sandbox import backend as sandbox_backend
from ..core.sandbox.subprocess_executor import ASTSecurityError, _check_ast_safety
from ..core.session import store
from ..core.session.manager import get_manager
from ..models.dto import GeneratedImage

pipeline_bp = Blueprint("pipelines", __name__)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@pipeline_bp.route("/ops", methods=["GET"])
@login_required
def list_ops():
    """Registry catalog + port types + coercion table (editor palette)."""
    cat = registry.catalog()
    cat["custom_nodes"] = [
        {"custom_node_id": cn["custom_node_id"], "name": cn["name"],
         "ports": cn["ports"], "params": cn["params"], "doc": cn["doc"]}
        for cn in store.list_custom_nodes(get_current_user_id())
    ]
    return jsonify(cat), 200


# ---------------------------------------------------------------------------
# Pipeline CRUD
# ---------------------------------------------------------------------------

@pipeline_bp.route("/pipelines", methods=["GET"])
@login_required
def list_pipelines():
    out = store.list_pipelines(get_current_user_id())
    for p in out:
        p["publication"] = None if p["is_preset"] else publishing.publication_for(p["pipeline_id"])
    return jsonify(out), 200


@pipeline_bp.route("/pipelines", methods=["POST"])
@login_required
def create_pipeline():
    """Create a pipeline, optionally cloning an existing one ({clone_from})."""
    data = request.get_json(force=True)
    user_id = get_current_user_id()
    now = datetime.utcnow().isoformat()
    pipeline_id = uuid.uuid4().hex

    clone_from = data.get("clone_from")
    if clone_from:
        src = store.load_pipeline(clone_from)
        # Clonable: your own pipelines, or any preset.
        if not src or (src["user_id"] is not None and src["user_id"] != user_id):
            return jsonify({"error": "Pipeline not found"}), 404
        title = (data.get("title") or f"Copy of {src['title']}").strip()
        store.save_pipeline(pipeline_id, user_id, title, src["description"],
                            src["graph"], now, now)
    else:
        title = (data.get("title") or "Untitled pipeline").strip()
        graph = data.get("graph") or {"nodes": [], "edges": []}
        store.save_pipeline(pipeline_id, user_id, title,
                            (data.get("description") or "").strip(), graph, now, now)

    return jsonify({"pipeline_id": pipeline_id}), 201


def _owned_pipeline(pipeline_id: str, user_id: str, *, for_write: bool):
    p = store.load_pipeline(pipeline_id)
    if not p:
        return None
    if p["user_id"] is None:
        return None if for_write else p     # presets: readable, never writable
    return p if p["user_id"] == user_id else None


@pipeline_bp.route("/pipelines/<pipeline_id>", methods=["GET"])
@login_required
def get_pipeline(pipeline_id: str):
    p = _owned_pipeline(pipeline_id, get_current_user_id(), for_write=False)
    if not p:
        return jsonify({"error": "Pipeline not found"}), 404
    p["publication"] = None if p["is_preset"] else publishing.publication_for(pipeline_id)
    return jsonify(p), 200


@pipeline_bp.route("/pipelines/<pipeline_id>", methods=["PATCH"])
@login_required
def update_pipeline(pipeline_id: str):
    user_id = get_current_user_id()
    p = _owned_pipeline(pipeline_id, user_id, for_write=True)
    if not p:
        return jsonify({"error": "Pipeline not found or read-only"}), 404

    data = request.get_json(force=True)
    title = (data.get("title") or p["title"]).strip()
    description = data.get("description", p["description"])
    graph = data.get("graph", p["graph"])
    store.save_pipeline(pipeline_id, user_id, title, description, graph,
                        p["created_at"], datetime.utcnow().isoformat())
    return jsonify({"status": "ok"}), 200


@pipeline_bp.route("/pipelines/<pipeline_id>", methods=["DELETE"])
@login_required
def delete_pipeline(pipeline_id: str):
    if not _owned_pipeline(pipeline_id, get_current_user_id(), for_write=True):
        return jsonify({"error": "Pipeline not found or read-only"}), 404
    store.delete_pipeline(pipeline_id)
    return "", 204


@pipeline_bp.route("/pipelines/validate", methods=["POST"])
@login_required
def validate_pipeline():
    """Static validation of a graph — used by the editor and the LLM loop."""
    data = request.get_json(force=True)
    graph = data.get("graph") or {}
    customs = {cn["custom_node_id"]: cn
               for cn in store.list_custom_nodes(get_current_user_id())}
    issues = runner.validate(graph, custom_nodes=customs)
    return jsonify({"valid": not issues, "issues": [i.to_dict() for i in issues]}), 200


# ---------------------------------------------------------------------------
# Published pipelines (instance-wide, versioned — D-035)
# ---------------------------------------------------------------------------

def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _publish_error(e: "publishing.PublishError"):
    body = {"error": str(e)}
    if e.issues:
        body["issues"] = e.issues
    return jsonify(body), e.status


@pipeline_bp.route("/pipelines/<pipeline_id>/publish", methods=["POST"])
@login_required
def publish_pipeline(pipeline_id: str):
    """Publish the saved state of one of your pipelines as its next version."""
    data = request.get_json(silent=True) or {}
    try:
        res = publishing.publish(get_current_user_id(), pipeline_id, data.get("changelog") or "",
                                 thumbnail=data.get("thumbnail", True) is not False)
    except publishing.PublishError as e:
        return _publish_error(e)
    return jsonify(res), 201 if res["created"] else 200


@pipeline_bp.route("/published", methods=["GET"])
@login_required
def list_published():
    return jsonify(publishing.list_public(get_current_user_id(), request.args.get("q"))), 200


@pipeline_bp.route("/published/<published_id>", methods=["GET"])
@login_required
def get_published(published_id: str):
    d = publishing.get(get_current_user_id(), published_id,
                       _int_or_none(request.args.get("version")))
    if not d:
        return jsonify({"error": "Published pipeline not found"}), 404
    return jsonify(d), 200


@pipeline_bp.route("/published/<published_id>/thumbnail", methods=["GET"])
@login_required
def published_thumbnail(published_id: str):
    """Card image: the author's latest output of the published graph (#8)."""
    from flask import send_file
    path = publishing.thumbnail_for(get_current_user_id(), published_id)
    if not path:
        return jsonify({"error": "No thumbnail"}), 404
    resp = send_file(path, mimetype="image/jpeg", max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@pipeline_bp.route("/published/<published_id>/unpublish", methods=["POST"])
@login_required
def unpublish_pipeline(published_id: str):
    if not publishing.unpublish(get_current_user_id(), published_id):
        return jsonify({"error": "Published pipeline not found"}), 404
    return jsonify({"status": "unpublished"}), 200


@pipeline_bp.route("/published/<published_id>/fork", methods=["POST"])
@login_required
def fork_published(published_id: str):
    data = request.get_json(silent=True) or {}
    try:
        res = publishing.fork(get_current_user_id(), published_id,
                              _int_or_none(data.get("version")), data.get("title"))
    except publishing.PublishError as e:
        return _publish_error(e)
    return jsonify(res), 201


# ---------------------------------------------------------------------------
# Custom nodes
# ---------------------------------------------------------------------------

@pipeline_bp.route("/custom-nodes", methods=["GET"])
@login_required
def list_custom_nodes():
    return jsonify(store.list_custom_nodes(get_current_user_id())), 200


@pipeline_bp.route("/custom-nodes", methods=["POST"])
@login_required
def create_custom_node():
    """Save a custom node. Code is AST-checked here (save time) with the same
    checker as run-code, because the pipeline runner execs it without an
    import whitelist."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    code = data.get("code") or ""
    ports = data.get("ports") or {}
    if not name or not code.strip():
        return jsonify({"error": "name and code are required"}), 400
    if not ports.get("inputs") or not ports.get("outputs"):
        return jsonify({"error": "ports must declare inputs and outputs"}), 400

    try:
        _check_ast_safety(code, strict_io=True)
    except (ASTSecurityError, SyntaxError) as e:
        return jsonify({"error": f"Rejected by sandbox policy: {e}"}), 400
    if "def node(" not in code:
        return jsonify({"error": "Code must define node(inputs, params)"}), 400

    existing = store.find_custom_node_by_name(get_current_user_id(), name)
    node_id = existing["custom_node_id"] if existing else uuid.uuid4().hex
    store.save_custom_node(node_id, get_current_user_id(), name, code, ports,
                           data.get("params") or [], (data.get("doc") or "").strip(),
                           datetime.utcnow().isoformat())
    return jsonify({"custom_node_id": node_id, "name": name}), 201


@pipeline_bp.route("/custom-nodes/<custom_node_id>", methods=["DELETE"])
@login_required
def delete_custom_node(custom_node_id: str):
    if not store.load_custom_node(custom_node_id):
        return jsonify({"error": "Custom node not found"}), 404
    store.delete_custom_node(custom_node_id, get_current_user_id())
    return "", 204


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

# Reuse the chat SSE queue infra so pipeline progress rides the same
# per-session stream the frontend already subscribes to.
def _push(session_id: str, event: dict) -> None:
    from .chat_routes import _push_event
    _push_event(session_id, event)


def _finish(session_id: str) -> None:
    from .chat_routes import _finish_stream
    _finish_stream(session_id)


def run_pipeline_for_session(session_id: str, graph: dict, title: str,
                             user_id: str, source_turn_index: int | None = None,
                             emit=None, custom_nodes: dict | None = None,
                             provenance: dict | None = None,
                             manage_status: bool = True) -> dict:
    """Execute a graph against every image attached to a session.

    Shared by POST /sessions/<id>/run-pipeline and the LLM loop's
    response_type="pipeline" path. Streams node progress over the session's
    SSE queue and persists finals as GeneratedImages (D-025 linkage kept via
    source_turn_index).

    emit: optional callable(event_dict) replacing the SSE push — used by the
    MCP server, which has no browser stream to feed.
    custom_nodes: resolve custom_node_ids from this dict instead of the
    runner's own library — a published pipeline's frozen snapshot (D-035).
    provenance: extra keys recorded with each output (e.g. published_id/version).
    manage_status: claim the session (idle -> running) for the run and release
    it after. False when the caller already holds it (the LLM loop).
    """
    push = emit or (lambda ev: _push(session_id, ev))
    manager = get_manager()
    sess = manager.get_session_for_user(session_id, user_id)
    if not sess:
        return {"error": "Session not found"}

    from .upload_routes import get_image_registry
    registry_images = get_image_registry()

    customs = custom_nodes if custom_nodes is not None else {
        cn["custom_node_id"]: cn for cn in store.list_custom_nodes(user_id)}
    issues = runner.validate(graph, custom_nodes=customs)
    if issues:
        return {"error": "Pipeline is invalid", "issues": [i.to_dict() for i in issues]}
    provenance_json = json.dumps({**provenance, "graph": graph} if provenance else graph, indent=2)

    if manage_status and not manager.try_start_run(session_id):
        return {"error": "Session is already running"}
    run_id = uuid.uuid4().hex[:8]
    outputs_made: list[dict] = []
    last_error = None

    try:
        for slot_idx, image_id in enumerate(sess.image_ids):
            meta = registry_images.get(image_id)
            if not meta:
                continue

            out_dir = config.OUTPUTS_DIR / session_id / f"pipe{run_id}" / f"slot{slot_idx}"
            node_dir = out_dir / "nodes"
            push({"event": "pipeline_start", "image_slot": slot_idx,
                  "image_id": image_id, "title": title,
                  "node_count": len(graph.get("nodes") or [])})

            result = sandbox_backend.run_pipeline(
                graph, meta.path, str(out_dir), str(node_dir), custom_nodes=customs,
                input_mode="RGB" if meta.channel_count >= 3 else "L",
            )

            for ev in result.get("events", []):
                push({"event": f"node_{ev['phase']}", "node_id": ev["node_id"],
                      "image_slot": slot_idx, "message": ev.get("message")})

            previews_rel = {}
            for nid, info in (result.get("previews") or {}).items():
                try:
                    rel = Path(info["path"]).relative_to(config.OUTPUTS_DIR)
                    previews_rel[nid] = f"/outputs/{rel.as_posix()}"
                except ValueError:
                    pass
            if previews_rel:
                push({"event": "node_previews", "image_slot": slot_idx,
                      "previews": previews_rel})

            if result.get("error"):
                last_error = result["error"]
                push({"event": "pipeline_error", "image_slot": slot_idx,
                      "failed_node": result.get("failed_node"),
                      "message": result["error"]})
                continue

            for final in result.get("finals", []):
                out_path = Path(final["path"])
                preview_path = out_path.parent / f"{out_path.stem}_preview.jpg"
                try:
                    generate_thumbnail(out_path, preview_path)
                except Exception:
                    preview_path = None

                image_out_id = uuid.uuid4().hex
                description = f"pipeline_{title}_slot{slot_idx}_{final['node_id']}"
                manager.add_output(session_id, GeneratedImage(
                    image_id=image_out_id,
                    session_id=session_id,
                    description=description,
                    path=str(out_path),
                    preview_path=str(preview_path) if preview_path and preview_path.exists() else "",
                    code=provenance_json,   # graph IS the provenance
                    source_turn_index=source_turn_index,
                    produced_at=datetime.utcnow(),
                    source_iteration=slot_idx,
                ))

                preview_url = None
                if preview_path and preview_path.exists():
                    rel = preview_path.relative_to(config.OUTPUTS_DIR)
                    preview_url = f"/outputs/{rel.as_posix()}"
                out_info = {"image_id": image_out_id, "description": description,
                            "preview_url": preview_url, "image_slot": slot_idx}
                outputs_made.append(out_info)
                push({"event": "output_saved", **out_info})
    finally:
        if manage_status:
            manager.set_status(session_id, "idle")

    return {"error": last_error, "outputs": outputs_made, "run_id": run_id}


@pipeline_bp.route("/sessions/<session_id>/run-pipeline", methods=["POST"])
@login_required
def run_pipeline_route(session_id: str):
    """Run a saved pipeline (or an ad-hoc graph) against a session's images."""
    user_id = get_current_user_id()
    sess = get_manager().get_session_for_user(session_id, user_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    if sess.status == "running":
        return jsonify({"error": "Session is already running"}), 409
    if not sess.image_ids:
        return jsonify({"error": "Session has no attached image"}), 400

    data = request.get_json(force=True)
    graph = data.get("graph")
    title = (data.get("title") or "adhoc").strip()
    customs = None
    provenance = None

    if data.get("published_id"):
        try:
            graph, customs, title, provenance = publishing.resolve_for_run(
                user_id, data["published_id"], _int_or_none(data.get("version")))
        except publishing.PublishError as e:
            return jsonify({"error": str(e)}), e.status
    elif data.get("pipeline_id"):
        p = _owned_pipeline(data["pipeline_id"], user_id, for_write=False)
        if not p:
            return jsonify({"error": "Pipeline not found"}), 404
        graph, title = p["graph"], p["title"]

    if not graph:
        return jsonify({"error": "pipeline_id, published_id or graph is required"}), 400

    if customs is None:
        customs = {cn["custom_node_id"]: cn for cn in store.list_custom_nodes(user_id)}
    issues = runner.validate(graph, custom_nodes=customs)
    if issues:
        return jsonify({"error": "Pipeline is invalid",
                        "issues": [i.to_dict() for i in issues]}), 400

    from .chat_routes import _sse_lock, _sse_queues
    with _sse_lock:
        _sse_queues[session_id] = queue.Queue()

    def _worker():
        try:
            run_pipeline_for_session(session_id, graph, title, user_id,
                                     custom_nodes=customs, provenance=provenance)
        finally:
            _finish(session_id)

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"status": "started", "session_id": session_id, "title": title}), 202
