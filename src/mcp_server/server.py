"""Photon MCP server (stdio or streamable HTTP) — lets any MCP client LLM drive Photon's pipeline layer.

The client LLM is the planner: it reads the op catalog, writes a pipeline
graph, validates it, runs it, looks at the result and iterates. Photon's own
LLM loop is NOT involved.

Scope (v1, deliberately narrow):
  * pipeline ops only — registry ops are trusted code;
  * existing custom nodes may be *used* by custom_node_id, but new ones can't be
    saved, and there is no run_code: both execute arbitrary Python, and the
    subprocess sandbox is not a boundary we expose to remote/injected prompts
    until DockerExecutor ships.

Identity — everything a caller creates shows up in that user's web UI:
  * HTTP: every request carries `Authorization: Bearer pk_...`, an API key the
    user minted at /settings/api-keys. Each call acts as the key's owner.
  * stdio: local single-user transport, no token. Acts as PHOTON_MCP_USER
    (username or email).

Shares volumes/ and sessions.db with the Flask app. Sessions are re-read from
SQLite on every call (see SessionManager.invalidate) so UI-side edits are seen.
"""

from __future__ import annotations

import base64
import binascii
import io
import mimetypes
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource, ToolAnnotations
from PIL import Image as PILImage

from src.backend.config import config
from src.backend.core.auth import api_keys
from src.backend.core.image.fetch import FetchError, fetch_url
from src.backend.core.image.ingest import IngestError, ingest_image_bytes
from src.backend.core.pipeline import publishing, registry, runner
from src.backend.core.session import store
from src.backend.core.session.manager import get_manager

# Importing api modules pulls in Flask (installed anyway) but runs no app.
# upload_routes rehydrates the in-memory image registry from SQLite on import;
# pipeline_routes holds run_pipeline_for_session, the one execution path.
from src.backend.api.upload_routes import get_image_registry
from src.backend.api.pipeline_routes import run_pipeline_for_session

PREVIEW_EDGE = int(os.getenv("PHOTON_MCP_PREVIEW_EDGE", "512"))
SERVER_VERSION = "0.1.0"
SERVER_DESCRIPTION = (
    "Photon: upload images and run validated image-processing pipelines built from "
    "40 trusted ops (filters, thresholding, morphology, colour, H&E stain deconvolution, "
    "model-based features), with previews returned inline."
)

_transport = "stdio"   # set by main()

INSTRUCTIONS = (
        "Photon runs image-processing pipelines (DAGs of trusted ops) on uploaded images.\n"
        "Remote clients upload with upload_image(data_base64=..., filename=...) or a public url, "
        "and fetch full-res results with download_output.\n"
        "Before building a graph, check list_pipelines(scope='public') for a colleague's "
        "published pipeline that already does the job; run it with run_pipeline(published_id=...).\n"
        "Workflow: upload_image -> create_session -> list_ops -> validate_pipeline -> "
        "run_pipeline -> inspect returned previews -> adjust and re-run.\n"
        "Graph JSON: {\"nodes\": [{\"id\": \"g\", \"op\": \"to_grayscale\", \"params\": {}}], "
        "\"edges\": [{\"from\": [\"input\", \"image\"], \"to\": [\"g\", \"image\"]}]}.\n"
        "'input' is the implicit source node; ['input','image'] is the session image. "
        "Port names and types come from list_ops and must match exactly. IMAGE_RGB does "
        "not auto-convert to IMAGE_GRAY: insert to_grayscale. MASK/FLOAT_MAP auto-convert "
        "to grayscale. Always validate_pipeline before run_pipeline."
    )

# Tools are collected here and registered on a server built per transport
# (build_server), because auth is fixed at MCPServer construction: HTTP gets a
# bearer-token verifier, stdio doesn't.
_TOOLS: list[tuple[Any, dict]] = []
_RO = ToolAnnotations(read_only_hint=True)   # lets clients auto-approve


def _tool(**opts):
    def deco(fn):
        _TOOLS.append((fn, opts))
        return fn
    return deco


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_stdio_user_id: str | None = None


def _uid() -> str:
    """The Photon user this call acts as.

    HTTP: the owner of the request's API key (set by the bearer verifier).
    stdio: PHOTON_MCP_USER — a local single-user transport with no token.
    """
    tok = get_access_token()
    if tok is not None:
        return tok.subject
    if _transport == "http":                 # verifier guarantees a token; belt and braces
        raise ToolError("Not authenticated.")
    global _stdio_user_id
    if _stdio_user_id is None:
        ident = os.getenv("PHOTON_MCP_USER", "").strip()
        if not ident:
            raise ToolError("PHOTON_MCP_USER is not set; set it to a Photon username or email.")
        user = store.find_user_by_username_or_email(ident)
        if not user:
            raise ToolError(f"No Photon user '{ident}'. Sign up in the web UI first.")
        _stdio_user_id = user["user_id"]
    return _stdio_user_id


def _session(session_id: str):
    manager = get_manager()
    manager.invalidate(session_id)          # another process may have written it
    sess = manager.get_session_for_user(session_id, _uid())
    if not sess:
        raise ToolError(f"Session not found: {session_id}")
    return sess


def _image_meta(image_id: str):
    """Registry lookup with SQLite fallback (image may have been uploaded via the UI
    after this process started)."""
    reg = get_image_registry()
    meta = reg.get(image_id)
    if meta is None:
        meta = store.load_image(image_id)
        if meta is not None:
            reg[image_id] = meta
    return meta


def _preview(path: str | Path | None) -> Image | None:
    """Small JPEG for the client LLM to look at. Full-res stays on disk."""
    if not path or not Path(path).is_file():
        return None
    try:
        im = PILImage.open(path)
        im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return Image(data=buf.getvalue(), format="jpeg")
    except Exception:
        return None


def _customs() -> dict[str, dict]:
    return {cn["custom_node_id"]: cn for cn in store.list_custom_nodes(_uid())}


def _readable_pipeline(pipeline_id: str) -> dict:
    p = store.load_pipeline(pipeline_id)
    if not p or (p["user_id"] is not None and p["user_id"] != _uid()):
        raise ToolError(f"Pipeline not found: {pipeline_id}")
    return p


# ---------------------------------------------------------------------------
# Images & sessions
# ---------------------------------------------------------------------------

def _path_roots() -> list[Path] | None:
    """Directories `upload_image(path=...)` may read from. None = anywhere (stdio).

    Over HTTP the caller is remote and possibly prompt-injected, and reads run
    as the server's OS user — so paths are refused unless PHOTON_MCP_PATH_ROOTS
    (os.pathsep-separated) allow-lists directories. Photon's own volumes/ is
    never readable that way: it holds every user's uploads and outputs.
    """
    if _transport != "http":
        return None
    raw = os.getenv("PHOTON_MCP_PATH_ROOTS", "")
    return [Path(r).expanduser().resolve() for r in raw.split(os.pathsep) if r.strip()]


def _read_local(path: str) -> tuple[bytes, str]:
    p = Path(path).expanduser().resolve()
    roots = _path_roots()
    if roots is not None:
        if not roots:
            raise ToolError("Server-side paths are disabled over HTTP; send the image as "
                            "data_base64 (with filename) or as a url instead.")
        if not any(p.is_relative_to(r) for r in roots):
            raise ToolError("Path is outside the directories this server allows "
                            "(PHOTON_MCP_PATH_ROOTS).")
        if p.is_relative_to(config.VOLUMES_DIR.resolve()):
            raise ToolError("Photon's data directory can't be read by path.")
    if not p.is_file():
        raise ToolError(f"File not found: {p}")
    return p.read_bytes(), p.name


@_tool(structured_output=False)
def upload_image(path: str | None = None, url: str | None = None,
                 data_base64: str | None = None, filename: str | None = None,
                 session_id: str | None = None) -> list:
    """Upload an image. Give exactly one source:
      * data_base64 + filename: the file's bytes, base64-encoded (works from any
        client; filename's extension sets the format);
      * url: an http(s) URL on the public internet;
      * path: a file on the Photon server's disk (stdio; over HTTP only inside
        directories the operator allow-listed).

    Supported: .png .jpg .jpeg .tif .tiff .npy .bmp .webp. If session_id is given,
    the image is also attached to that session. Returns image_id, metadata
    (shape, dtype, value range, guessed kind) and a preview.
    """
    if sum(map(bool, (path, url, data_base64))) != 1:
        raise ToolError("Provide exactly one of 'path', 'url' or 'data_base64'.")
    if path:
        data, name = _read_local(path)
    elif url:
        try:
            data, name = fetch_url(url)
        except FetchError as e:
            raise ToolError(str(e))
    else:
        if not filename:
            raise ToolError("data_base64 needs a filename (e.g. 'scan.png') for its format.")
        b64 = data_base64.strip()
        if b64.startswith("data:") and "," in b64:       # tolerate data: URIs
            b64 = b64.split(",", 1)[1]
        if len(b64) * 3 // 4 > config.MAX_UPLOAD_BYTES:
            raise ToolError(f"Image too large (max {config.MAX_UPLOAD_MB} MB).")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("data_base64 is not valid base64.")
        name = Path(filename).name

    if session_id:
        _session(session_id)                # ownership check before writing anything
    try:
        meta = ingest_image_bytes(data, name)
    except IngestError as e:
        raise ToolError(str(e))
    get_image_registry()[meta.image_id] = meta
    if session_id:
        get_manager().add_image(session_id, meta.image_id)

    info = {"image_id": meta.image_id, "metadata": _meta_public(meta),
            "attached_to": session_id}
    return [info, *filter(None, [_preview(meta.preview_path)])]


def _meta_public(meta) -> dict:
    d = meta.to_dict()
    d.pop("preview_path", None)
    return d


@_tool()
def create_session(title: str = "MCP session", image_ids: list[str] | None = None) -> dict:
    """Create a Photon session (visible in the web UI) with optional uploaded images.

    Pipelines run against every image attached to the session.
    """
    image_ids = image_ids or []
    missing = [i for i in image_ids if _image_meta(i) is None]
    if missing:
        raise ToolError(f"Unknown image_ids: {missing}")
    sess = get_manager().create_session(user_id=_uid(), image_ids=image_ids, title=title)
    return {"session_id": sess.session_id, "title": sess.title, "image_ids": sess.image_ids}


@_tool()
def attach_image(session_id: str, image_id: str) -> dict:
    """Attach an already-uploaded image to a session."""
    _session(session_id)
    if _image_meta(image_id) is None:
        raise ToolError(f"Unknown image_id: {image_id}")
    sess = get_manager().add_image(session_id, image_id)
    return {"session_id": session_id, "image_ids": sess.image_ids}


@_tool(annotations=_RO)
def list_sessions() -> list[dict]:
    """List this user's sessions, most recently updated first."""
    return get_manager().list_sessions(_uid())


@_tool(annotations=_RO)
def get_session(session_id: str) -> dict:
    """Session details: attached images (with metadata) and produced outputs."""
    sess = _session(session_id)
    images = []
    for iid in sess.image_ids:
        m = _image_meta(iid)
        images.append({"image_id": iid, "metadata": _meta_public(m) if m else None})
    outputs = [{"output_id": o.image_id, "description": o.description, "path": o.path,
                "produced_at": o.produced_at.isoformat()} for o in sess.outputs]
    return {"session_id": sess.session_id, "title": sess.title, "status": sess.status,
            "images": images, "outputs": outputs}


@_tool(structured_output=False, annotations=_RO)
def get_output(session_id: str, output_id: str) -> list:
    """Fetch one output: full-res file path, the graph/code that produced it, and a preview."""
    sess = _session(session_id)
    out = next((o for o in sess.outputs if o.image_id == output_id), None)
    if not out:
        raise ToolError(f"Output not found: {output_id}")
    info = {"output_id": out.image_id, "description": out.description, "path": out.path,
            "provenance": out.code, "produced_at": out.produced_at.isoformat()}
    return [info, *filter(None, [_preview(out.preview_path or out.path)])]


@_tool(structured_output=False, annotations=_RO)
def download_output(session_id: str, output_id: str) -> list:
    """Full-resolution output file as an embedded resource (base64 blob), for
    clients that can't read the server's disk. get_output's preview is a
    downscaled JPEG; this is the actual saved file. Capped at
    PHOTON_MCP_MAX_DOWNLOAD_MB (default 25)."""
    sess = _session(session_id)
    out = next((o for o in sess.outputs if o.image_id == output_id), None)
    if not out or not out.path or not Path(out.path).is_file():
        raise ToolError(f"Output not found: {output_id}")
    p = Path(out.path)
    cap_mb = int(os.getenv("PHOTON_MCP_MAX_DOWNLOAD_MB", "25"))
    if p.stat().st_size > cap_mb * 1024 * 1024:
        raise ToolError(f"Output is {p.stat().st_size / 2**20:.1f} MB, over the "
                        f"{cap_mb} MB download cap; use get_output's path or the web UI.")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    blob = BlobResourceContents(uri=f"photon://sessions/{session_id}/outputs/{output_id}/{p.name}",
                                mime_type=mime,
                                blob=base64.b64encode(p.read_bytes()).decode("ascii"))
    return [{"output_id": output_id, "filename": p.name, "mime_type": mime,
             "bytes": p.stat().st_size},
            EmbeddedResource(type="resource", resource=blob)]


@_tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
def detach_image(session_id: str, image_id: str) -> dict:
    """Detach an image from a session. The uploaded image itself is kept (other
    sessions may use it); future runs just skip it."""
    sess = _session(session_id)
    if image_id not in sess.image_ids:
        raise ToolError(f"Image {image_id} is not attached to session {session_id}.")
    sess = get_manager().remove_image(session_id, image_id)
    return {"session_id": session_id, "image_ids": sess.image_ids}


@_tool(annotations=ToolAnnotations(destructive_hint=True))
def delete_session(session_id: str, confirm_title: str) -> dict:
    """Permanently delete a session: its outputs, history, share links, and any
    source image no other session uses. Irreversible. confirm_title must equal
    the session's current title exactly (see get_session) — a guard against
    deleting the wrong session."""
    sess = _session(session_id)
    if confirm_title != sess.title:
        raise ToolError(f"confirm_title does not match this session's title ({sess.title!r}); "
                        "nothing deleted.")
    deleted = get_manager().delete_session(session_id, _uid())
    if deleted is None:
        raise ToolError(f"Session not found: {session_id}")
    reg = get_image_registry()
    for iid in deleted:
        reg.pop(iid, None)
    return {"deleted": True, "session_id": session_id, "images_deleted": deleted}


# ---------------------------------------------------------------------------
# Ops & pipelines
# ---------------------------------------------------------------------------

@_tool(annotations=_RO)
def list_ops(category: str | None = None) -> dict:
    """Op catalog: every op's typed input/output ports and params, the port types,
    the implicit coercion table, and this user's saved custom nodes (usable via
    {"id": ..., "custom_node_id": ...}). Optionally filter by category.
    """
    cat = registry.catalog()
    if category:
        cat["ops"] = [o for o in cat["ops"] if o.get("category") == category]
    cat["custom_nodes"] = [
        {"custom_node_id": cn["custom_node_id"], "name": cn["name"],
         "ports": cn["ports"], "params": cn["params"], "doc": cn["doc"]}
        for cn in store.list_custom_nodes(_uid())
    ]
    return cat


@_tool(annotations=_RO)
def validate_pipeline(graph: dict) -> dict:
    """Statically check a pipeline graph (unknown ops, port names/types, cycles,
    params, unconnected inputs). Returns {valid, issues[]}. Nothing runs."""
    issues = runner.validate(graph, custom_nodes=_customs())
    return {"valid": not issues, "issues": [i.to_dict() for i in issues]}


@_tool(structured_output=False)
async def run_pipeline(session_id: str, ctx: Context, graph: dict | None = None,
                       pipeline_id: str | None = None, published_id: str | None = None,
                       version: int | None = None, title: str = "mcp") -> list:
    """Run a pipeline on every image in the session. Pass exactly one of: an
    ad-hoc `graph`, a saved `pipeline_id` (yours or a preset), or a
    `published_id` (anyone's public pipeline; optional `version`, default
    latest). Outputs are saved to the session (visible in the web UI). Returns
    per-output ids/paths plus previews, or the error and failing node so you
    can fix the graph and retry.
    """
    if sum(map(bool, (graph, pipeline_id, published_id))) != 1:
        raise ToolError("Provide exactly one of 'graph', 'pipeline_id' or 'published_id'.")
    sess = _session(session_id)
    if sess.status == "running":
        raise ToolError("Session is already running a job.")
    if not sess.image_ids:
        raise ToolError("Session has no attached image; use upload_image/attach_image.")
    customs = provenance = None
    if pipeline_id:
        p = _readable_pipeline(pipeline_id)
        graph, title = p["graph"], p["title"]
    elif published_id:
        try:
            graph, customs, title, provenance = publishing.resolve_for_run(
                _uid(), published_id, version)
        except publishing.PublishError as e:
            raise ToolError(str(e))
    for iid in sess.image_ids:                 # registry may predate UI uploads
        _image_meta(iid)

    n_images = len(sess.image_ids)
    errors: list[dict] = []

    def emit(ev: dict) -> None:
        kind = ev.get("event")
        if kind == "pipeline_error":
            errors.append({"image_slot": ev.get("image_slot"),
                           "failed_node": ev.get("failed_node"), "message": ev.get("message")})
        if kind in ("pipeline_start", "output_saved", "pipeline_error"):
            done = ev.get("image_slot", 0) + (0 if kind == "pipeline_start" else 1)
            try:
                anyio.from_thread.run(ctx.report_progress, done, n_images,
                                      f"{kind} slot {ev.get('image_slot')}")
            except Exception:
                pass                           # progress is best-effort

    uid = _uid()
    result = await anyio.to_thread.run_sync(
        lambda: run_pipeline_for_session(session_id, graph, title, uid, emit=emit,
                                         custom_nodes=customs, provenance=provenance))

    if result.get("issues"):
        return [{"ok": False, "error": result["error"], "issues": result["issues"]}]

    sess = _session(session_id)
    by_id = {o.image_id: o for o in sess.outputs}
    content: list[Any] = []
    outputs = []
    for o in result.get("outputs", []):
        rec = by_id.get(o["image_id"])
        outputs.append({"output_id": o["image_id"], "image_slot": o["image_slot"],
                        "description": o["description"], "path": rec.path if rec else None})
        if rec:
            img = _preview(rec.preview_path or rec.path)
            if img:
                content.append(img)
    summary = {"ok": not errors and bool(outputs), "run_id": result.get("run_id"),
               "outputs": outputs, "errors": errors or None}
    return [summary, *content]


@_tool(annotations=_RO)
def list_pipelines(scope: str = "all", query: str | None = None) -> dict:
    """Pipelines you can run.

    scope: "mine" (your library + read-only presets), "public" (published by
    anyone on this Photon instance, run via published_id), or "all" (both).
    query: optional case-insensitive filter on title/description/author.
    Prefer an existing public pipeline over writing a new graph when one fits.
    """
    if scope not in ("mine", "public", "all"):
        raise ToolError("scope must be 'mine', 'public' or 'all'.")
    q = (query or "").lower()
    out: dict[str, Any] = {}
    if scope in ("mine", "all"):
        out["mine"] = [
            {"pipeline_id": p["pipeline_id"], "title": p["title"],
             "description": p.get("description"), "preset": p["user_id"] is None,
             "publication": None if p["user_id"] is None else publishing.publication_for(p["pipeline_id"])}
            for p in store.list_pipelines(_uid())
            if not q or q in p["title"].lower() or q in (p.get("description") or "").lower()]
    if scope in ("public", "all"):
        out["public"] = [
            {"published_id": p["published_id"], "title": p["title"], "description": p["description"],
             "owner": p["owner"], "is_mine": p["is_mine"], "latest_version": p["latest_version"],
             "node_count": p["node_count"], "uses_custom_nodes": p["uses_custom_nodes"],
             "unpublished": bool(p["unpublished_at"])}
            for p in publishing.list_public(_uid(), query)]
    return out


@_tool(annotations=_RO)
def get_pipeline(pipeline_id: str | None = None, published_id: str | None = None,
                 version: int | None = None) -> dict:
    """Full pipeline including its graph. Give pipeline_id (your library or a
    preset) or published_id (+ optional version; includes the version list and
    the frozen custom node code the version runs)."""
    if bool(pipeline_id) == bool(published_id):
        raise ToolError("Provide exactly one of 'pipeline_id' or 'published_id'.")
    if pipeline_id:
        p = _readable_pipeline(pipeline_id)
        if p["user_id"] is not None:
            p["publication"] = publishing.publication_for(pipeline_id)
        return p
    d = publishing.get(_uid(), published_id, version)
    if not d:
        raise ToolError(f"Published pipeline (or that version) not found: {published_id}")
    return d


@_tool()
def publish_pipeline(pipeline_id: str, changelog: str = "") -> dict:
    """Publish one of your saved pipelines to everyone on this Photon instance
    as its next immutable version (graph + the code of every custom node it
    uses are frozen). Re-publishing the same pipeline adds a version; if
    nothing changed, no new version is made. Must validate first."""
    try:
        return publishing.publish(_uid(), pipeline_id, changelog)
    except publishing.PublishError as e:
        if e.issues:
            return {"published": False, "error": str(e), "issues": e.issues}
        raise ToolError(str(e))


@_tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
def unpublish_pipeline(published_id: str) -> dict:
    """Hide one of your publications from others (versions are kept; publishing
    the source pipeline again restores it)."""
    if not publishing.unpublish(_uid(), published_id):
        raise ToolError(f"Not found among your publications: {published_id}")
    return {"unpublished": True, "published_id": published_id}


@_tool()
def fork_pipeline(published_id: str, version: int | None = None,
                  title: str | None = None) -> dict:
    """Copy a published version into your library as an editable pipeline. Its
    custom nodes are imported into your library too (reused if you already
    have an identical one). Returns the new pipeline_id."""
    try:
        return publishing.fork(_uid(), published_id, version, title)
    except publishing.PublishError as e:
        raise ToolError(str(e))


@_tool()
def save_pipeline(title: str, graph: dict, description: str = "") -> dict:
    """Save a (valid) graph as a new pipeline in the user's library — it then
    appears in the web UI's pipeline editor."""
    issues = runner.validate(graph, custom_nodes=_customs())
    if issues:
        return {"saved": False, "issues": [i.to_dict() for i in issues]}
    pid = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    store.save_pipeline(pid, _uid(), title.strip() or "Untitled pipeline",
                        description.strip(), graph, now, now)
    return {"saved": True, "pipeline_id": pid}


class ApiKeyVerifier:
    """Bearer-token verifier backed by Photon API keys (core/auth/api_keys.py)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        row = await anyio.to_thread.run_sync(api_keys.verify_key, token)
        if row is None:
            return None
        exp = None
        if row["expires_at"]:
            exp = int(datetime.fromisoformat(row["expires_at"]).timestamp())
        # subject = Photon user_id; client_id = key_id (one MCP principal per key).
        return AccessToken(token=token, client_id=row["key_id"], subject=row["user_id"],
                           scopes=[], expires_at=exp)


def build_server(*, http_auth_url: str | None = None) -> MCPServer:
    """Build the server. http_auth_url (the public MCP URL) enables bearer auth."""
    kwargs: dict[str, Any] = {}
    if http_auth_url:
        kwargs["token_verifier"] = ApiKeyVerifier()
        kwargs["auth"] = AuthSettings(
            # Keys are minted by the Photon web app, not an OAuth server; the issuer
            # URL is only advertised in protected-resource metadata.
            issuer_url=os.getenv("PHOTON_PUBLIC_URL") or http_auth_url.rsplit("/mcp", 1)[0],
            resource_server_url=http_auth_url,
            validate_token_resource=False,
        )
    server = MCPServer(name="photon", title="Photon image processing",
                       description=SERVER_DESCRIPTION, version=SERVER_VERSION,
                       instructions=INSTRUCTIONS, **kwargs)
    for fn, opts in _TOOLS:
        server.tool(**opts)(fn)
    return server


def main() -> None:
    import argparse

    global _transport
    ap = argparse.ArgumentParser(prog="python -m src.mcp_server",
                                 description="Photon MCP server")
    ap.add_argument("--transport", choices=["stdio", "http"],
                    default=os.getenv("PHOTON_MCP_TRANSPORT", "stdio"))
    ap.add_argument("--host", default=os.getenv("PHOTON_MCP_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("PHOTON_MCP_PORT", "8890")))
    args = ap.parse_args()
    _transport = args.transport

    # Diagnostics go to stderr: in stdio mode stdout is the MCP channel.
    if args.transport == "stdio":
        if not os.getenv("PHOTON_MCP_USER"):
            print("photon-mcp: PHOTON_MCP_USER not set; tools will error until it is.",
                  file=sys.stderr)
        build_server().run("stdio")
        return

    # Streamable HTTP: every request needs an API key (Authorization: Bearer pk_...),
    # minted per user at /settings/api-keys in the web UI. PHOTON_MCP_USER is ignored.
    public_url = (os.getenv("PHOTON_MCP_PUBLIC_URL")
                  or f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/mcp")
    print(f"photon-mcp: streamable HTTP on {args.host}:{args.port}/mcp "
          f"(public URL {public_url}); API key required", file=sys.stderr)
    build_server(http_auth_url=public_url).run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
