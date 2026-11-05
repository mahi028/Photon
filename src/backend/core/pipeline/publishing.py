"""Published pipelines: instance-wide, immutable, versioned snapshots (D-035).

A user's pipeline lives in their library and is freely editable. Publishing
freezes it into a numbered version that every user on the instance can run
or fork:

  * the graph is copied as-is;
  * every custom node the graph uses is copied in full (code, ports, params,
    doc) into the version, keyed by its original custom_node_id. Runs
    resolve custom nodes from that snapshot, never from anyone's live
    library, so the author editing or deleting a node can't break people
    already using version N. (Custom nodes are otherwise per-user, which is
    exactly what blocked sharing before.)

Re-publishing the same pipeline appends version N+1 to the same publication;
an identical snapshot is not re-published. Unpublishing hides it from others
(owners still see it and can re-publish); versions are never deleted, so a
run's provenance stays resolvable.

Trust: instance users are trusted colleagues (D-035). Custom node code runs
in the same rlimited subprocess as the author's own runs; the AST check is
re-applied at publish time in case policy tightened since the node was saved.

No HTTP here: used by api/pipeline_routes.py and the MCP server.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from ..sandbox.subprocess_executor import ASTSecurityError, _check_ast_safety
from ..session import store
from . import runner

MAX_CHANGELOG = 500


class PublishError(Exception):
    """status mirrors the HTTP code the routes return."""

    def __init__(self, message: str, status: int = 400, issues: list | None = None):
        super().__init__(message)
        self.status = status
        self.issues = issues


def _node_snapshot(cn: dict) -> dict:
    return {"name": cn["name"], "code": cn["code"], "ports": cn["ports"],
            "params": cn["params"], "doc": cn["doc"]}


def _used_custom_ids(graph: dict) -> list[str]:
    return sorted({n["custom_node_id"] for n in graph.get("nodes") or [] if n.get("custom_node_id")})


def _owner_label(pub: dict) -> str:
    return pub.get("owner_display_name") or pub.get("owner_username") or "unknown"


def publish(user_id: str, pipeline_id: str, changelog: str = "") -> dict:
    """Publish (or re-publish) one of the user's own pipelines.

    Returns {published_id, version, created} where created=False means the
    snapshot was identical to the latest version and nothing new was written.
    """
    p = store.load_pipeline(pipeline_id)
    if not p or p["user_id"] != user_id:
        raise PublishError("Pipeline not found (only your own pipelines can be published).", 404)
    graph = p["graph"]
    if not graph.get("nodes"):
        raise PublishError("Pipeline is empty.")

    own_customs = {cn["custom_node_id"]: cn for cn in store.list_custom_nodes(user_id)}
    issues = runner.validate(graph, custom_nodes=own_customs)
    if issues:
        raise PublishError("Pipeline is invalid; fix it before publishing.", 400,
                           [i.to_dict() for i in issues])

    snapshot: dict[str, dict] = {}
    for cid in _used_custom_ids(graph):
        cn = own_customs[cid]                    # validate() guarantees presence
        try:
            _check_ast_safety(cn["code"])
        except (ASTSecurityError, SyntaxError) as e:
            raise PublishError(f"Custom node '{cn['name']}' is rejected by the sandbox policy: {e}")
        snapshot[cid] = _node_snapshot(cn)

    title = p["title"].strip() or "Untitled pipeline"
    description = (p["description"] or "").strip()
    existing = store.find_published_by_source(pipeline_id)
    if existing:
        latest = store.load_published_version(existing["published_id"], existing["latest_version"])
        unchanged = (latest["graph"] == graph and latest["custom_nodes"] == snapshot
                     and latest["title"] == title and latest["description"] == description)
        if unchanged and not existing["unpublished_at"]:
            return {"published_id": existing["published_id"],
                    "version": existing["latest_version"], "created": False}
        published_id = existing["published_id"]
    else:
        published_id = uuid.uuid4().hex

    version = store.add_published_version(
        published_id, user_id, pipeline_id, title, description, graph, snapshot,
        (changelog or "").strip()[:MAX_CHANGELOG], datetime.utcnow().isoformat())
    return {"published_id": published_id, "version": version, "created": True}


def unpublish(user_id: str, published_id: str) -> bool:
    return store.set_published_unpublished(published_id, user_id, datetime.utcnow().isoformat())


def _visible(pub: Optional[dict], viewer_id: str) -> Optional[dict]:
    if not pub:
        return None
    if pub["unpublished_at"] and pub["owner_id"] != viewer_id:
        return None
    return pub


def list_public(viewer_id: str, query: str | None = None) -> list[dict]:
    """Live publications (+ the viewer's own unpublished ones), optionally
    filtered by a case-insensitive substring over title/description/author."""
    pubs = store.list_published_pipelines(include_unpublished_of=viewer_id)
    if query:
        q = query.lower()
        pubs = [p for p in pubs if q in p["title"].lower() or q in p["description"].lower()
                or q in (p["owner_username"] or "").lower()
                or q in (p["owner_display_name"] or "").lower()]
    for p in pubs:
        p["owner"] = _owner_label(p)
        p["is_mine"] = p["owner_id"] == viewer_id
    return pubs


def get(viewer_id: str, published_id: str, version: int | None = None) -> Optional[dict]:
    """A publication with one version's full graph and custom node snapshot
    (code included: trusted instance, and users should see what they run)."""
    pub = _visible(store.load_published_pipeline(published_id), viewer_id)
    if not pub:
        return None
    v = store.load_published_version(published_id, version or pub["latest_version"])
    if not v:
        return None
    return {**pub, "owner": _owner_label(pub), "is_mine": pub["owner_id"] == viewer_id,
            "version": v["version"], "graph": v["graph"], "custom_nodes": v["custom_nodes"],
            "changelog": v["changelog"], "published_at": v["published_at"],
            "version_title": v["title"],
            "versions": store.list_published_versions(published_id)}


def resolve_for_run(viewer_id: str, published_id: str,
                    version: int | None = None) -> tuple[dict, dict[str, dict], str, dict]:
    """(graph, custom_nodes, run_title, provenance) for running a publication.
    Custom nodes come from the snapshot, shaped like store rows so the runner
    accepts them; provenance is recorded with each output."""
    d = get(viewer_id, published_id, version)
    if not d:
        raise PublishError("Published pipeline (or that version) not found.", 404)
    customs = {cid: {"custom_node_id": cid, **cn} for cid, cn in d["custom_nodes"].items()}
    provenance = {"published_id": published_id, "version": d["version"],
                  "title": d["version_title"], "owner": d["owner"]}
    return d["graph"], customs, f"{d['version_title']} v{d['version']}", provenance


def publication_for(pipeline_id: str) -> Optional[dict]:
    """Publish status of a library pipeline, for the owner's UI."""
    pub = store.find_published_by_source(pipeline_id)
    if not pub:
        return None
    return {"published_id": pub["published_id"], "latest_version": pub["latest_version"],
            "latest_published_at": pub["latest_published_at"],
            "unpublished": bool(pub["unpublished_at"])}


def _candidate_names(original: str, base: str):
    yield original
    yield base
    n = 2
    while True:
        yield f"{base} {n}"
        n += 1


def fork(user_id: str, published_id: str, version: int | None = None,
         title: str | None = None) -> dict:
    """Copy a published version into the user's library as an editable pipeline.

    Snapshot custom nodes are imported into the user's own library. A node
    whose name already exists there with identical content is reused;
    otherwise it is imported under a free name ("name (from alice)", "... 2").
    Returns {pipeline_id, imported_nodes: [{name, custom_node_id, reused}]}.
    """
    d = get(user_id, published_id, version)
    if not d:
        raise PublishError("Published pipeline (or that version) not found.", 404)
    now = datetime.utcnow().isoformat()

    id_map: dict[str, str] = {}
    imported = []
    for old_id, cn in d["custom_nodes"].items():
        # First free-or-identical name wins: original, then "(from owner)", then numbered.
        base = f"{cn['name']} (from {d['owner_username']})"
        for name in _candidate_names(cn["name"], base):
            hit = store.find_custom_node_by_name(user_id, name)
            if hit is None:
                new_id = uuid.uuid4().hex
                store.save_custom_node(new_id, user_id, name, cn["code"], cn["ports"],
                                       cn["params"], cn["doc"], now)
                id_map[old_id] = new_id
                imported.append({"name": name, "custom_node_id": new_id, "reused": False})
                break
            if _node_snapshot(hit) == {**cn, "name": name}:
                id_map[old_id] = hit["custom_node_id"]
                imported.append({"name": name, "custom_node_id": hit["custom_node_id"], "reused": True})
                break

    graph = {**d["graph"], "nodes": [
        {**n, "custom_node_id": id_map[n["custom_node_id"]]} if n.get("custom_node_id") else n
        for n in d["graph"].get("nodes") or []]}
    pipeline_id = uuid.uuid4().hex
    desc = d["description"]
    credit = f"Forked from \"{d['version_title']}\" v{d['version']} by {d['owner']}."
    store.save_pipeline(pipeline_id, user_id,
                        (title or "").strip() or f"{d['version_title']} (fork)",
                        f"{desc}\n\n{credit}".strip(), graph, now, now)
    return {"pipeline_id": pipeline_id, "imported_nodes": imported}
