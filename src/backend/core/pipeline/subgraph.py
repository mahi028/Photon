"""Published pipelines as nodes (D-045).

A node can be a whole published pipeline, pinned to one version:

  {"id": "seg", "pipeline": {"published_id": "<id>", "version": 3},
   "params": {"t.classes": 4}}

  inputs   `image`, when the inner graph reads the source image
  outputs  the inner graph's terminal outputs (nodes nothing else reads),
           named by port ("labels"), or "<inner node>.<port>" when two
           terminal nodes share a port name
  params   overrides "<inner node>.<param>"; unset = the published value

Before validation and before a run the node is EXPANDED in the parent:
inner nodes become "<node id>/<inner id>", edges from the inner `input` are
rewired to whatever feeds the node's `image`, and outer edges from the node's
outputs to the inner nodes producing them. The inner version's frozen custom
nodes come along under "<published_id>@<version>:<custom_node_id>", so they
never collide with (or are replaced by) the viewer's own library. The sandbox
child only ever sees a plain graph.

Versions are immutable, so a pinned node always runs the same thing; an
author unpublishing makes it fail with a clear message (fork it to keep it).
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from . import runner
from .runner import INPUT_NODE_ID, Issue

MAX_DEPTH = 4
MAX_NODES = 400
SEP = "/"


class SubgraphError(Exception):
    def __init__(self, node_id: Optional[str], message: str):
        super().__init__(message)
        self.node_id = node_id


def is_sub(node: dict) -> bool:
    return isinstance(node.get("pipeline"), dict)


def has_subs(graph: dict) -> bool:
    return any(is_sub(n) for n in (graph or {}).get("nodes") or [])


def _load(viewer_id: str, ref: dict, node_id: str) -> dict:
    from . import publishing
    pid = ref.get("published_id")
    try:
        version = int(ref.get("version"))
    except (TypeError, ValueError):
        raise SubgraphError(node_id, "A pipeline node needs pipeline.version (it is pinned to one version).")
    d = publishing.get(viewer_id, str(pid or ""), version) if pid else None
    if not d or d["version"] != version:
        raise SubgraphError(node_id, f"Published pipeline {pid} v{version} was not found or was "
                                     "unpublished by its author. Fork it to keep a copy.")
    return d


def _customs_of(d: dict) -> tuple[dict[str, str], dict[str, dict]]:
    """(old id -> namespaced id, namespaced id -> runner custom-node row)."""
    rename, rows = {}, {}
    for cid, cn in (d.get("custom_nodes") or {}).items():
        new = f"{d['published_id']}@{d['version']}:{cid}"
        rename[cid] = new
        rows[new] = {"custom_node_id": new, **cn}
    return rename, rows


def interface(d: dict, viewer_id: str, depth: int = 0) -> dict:
    """Ports and overridable params of one published version, for editors and
    validation. Inner sub-pipeline nodes are expanded first."""
    graph, customs = _expand_graph(d["graph"], viewer_id, {}, depth + 1)
    rename, rows = _customs_of(d)
    customs.update(rows)
    for n in graph["nodes"]:
        if n.get("custom_node_id") in rename:
            n["custom_node_id"] = rename[n["custom_node_id"]]
    nodes = {n["id"]: n for n in graph["nodes"]}
    consumed = {e["from"][0] for e in graph["edges"]}
    reads_input = any(e["from"][0] == INPUT_NODE_ID for e in graph["edges"])

    terminal: list[tuple[str, Any]] = []      # (inner id, Port)
    params: list[dict] = []
    for nid, n in nodes.items():
        try:
            ins, outs, ps, label = runner._node_ports(n, customs)
        except KeyError:
            raise SubgraphError(None, f"Published pipeline uses an op that no longer exists: "
                                      f"{n.get('op') or n.get('custom_node_id')}")
        if nid not in consumed:
            terminal.extend((nid, p) for p in outs)
        set_vals = n.get("params") or {}
        for p in ps:
            params.append({"name": f"{nid}.{p.name}", "kind": p.kind,
                           "default": set_vals.get(p.name, p.default),
                           "choices": getattr(p, "choices", None), "min": getattr(p, "min", None),
                           "max": getattr(p, "max", None), "nullable": getattr(p, "nullable", False),
                           "doc": f"{label}: {getattr(p, 'doc', '') or p.name}"})
    names = [p.name for _, p in terminal]
    bare = len(set(names)) == len(names)
    outputs = [{"name": p.name if bare else f"{nid}.{p.name}", "type": p.type.value,
                "inner": [nid, p.name]} for nid, p in terminal]
    inputs = [{"name": "image", "type": "IMAGE_ANY", "optional": False}] if reads_input else []
    return {"published_id": d["published_id"], "version": d["version"], "title": d["version_title"],
            "owner": d.get("owner"), "doc": (d.get("description") or "")[:500],
            "inputs": inputs, "outputs": outputs, "params": params,
            "_graph": graph, "_customs": customs}


def _expand_graph(graph: dict, viewer_id: str, customs: dict, depth: int,
                  outer: dict | None = None) -> tuple[dict, dict]:
    """Return (plain graph, custom nodes it needs beyond `customs`). Fills
    `outer` with {sub id: {output port: [inner node, port]}}."""
    if depth > MAX_DEPTH:
        raise SubgraphError(None, f"Pipelines nested more than {MAX_DEPTH} deep.")
    nodes_out: list[dict] = []
    extra: dict[str, dict] = {}
    # outer (node, port) -> inner (node, port), and node -> what feeds its `image`
    out_map: dict[tuple[str, str], tuple[str, str]] = {}
    subs: dict[str, dict] = {}
    for n in graph.get("nodes") or []:
        if not is_sub(n):
            nodes_out.append(n)
            continue
        sid = n.get("id") or ""
        d = _load(viewer_id, n["pipeline"], sid)
        try:
            itf = interface(d, viewer_id, depth)
        except SubgraphError as e:
            raise SubgraphError(sid, f"Inside '{d['version_title']}' v{d['version']}: {e}")
        subs[sid] = itf
        extra.update(itf["_customs"])
        inner_ids = {m["id"] for m in itf["_graph"]["nodes"]}
        overrides = n.get("params") or {}
        for key in overrides:
            inner, _, pname = str(key).partition(".")
            if inner not in inner_ids or not pname:
                raise SubgraphError(sid, f"Unknown param '{key}'. Params look like '<inner node>.<param>': "
                                         + ", ".join(p["name"] for p in itf["params"][:30]))
        for m in itf["_graph"]["nodes"]:
            m2 = copy.deepcopy(m)
            m2["id"] = f"{sid}{SEP}{m['id']}"
            ps = dict(m2.get("params") or {})
            for key, v in overrides.items():
                inner, _, pname = str(key).partition(".")
                if inner == m["id"]:
                    ps[pname] = v
            m2["params"] = ps
            m2.pop("pos", None)
            nodes_out.append(m2)
        for o in itf["outputs"]:
            out_map[(sid, o["name"])] = (f"{sid}{SEP}{o['inner'][0]}", o["inner"][1])
        if outer is not None:
            outer[sid] = {o["name"]: [f"{sid}{SEP}{o['inner'][0]}", o["inner"][1]] for o in itf["outputs"]}

    if not subs:
        return copy.deepcopy(graph), extra

    feeds: dict[str, list] = {}               # sub id -> source feeding its `image`
    edges_out: list[dict] = []
    for e in graph.get("edges") or []:
        try:
            (src, sport), (dst, dport) = e["from"], e["to"]
        except (KeyError, ValueError, TypeError):
            edges_out.append(e)                # runner.validate reports it
            continue
        if src in subs:
            if (src, sport) not in out_map:
                raise SubgraphError(src, f"No output '{sport}'. Outputs: "
                                         + ", ".join(o["name"] for o in subs[src]["outputs"]))
            src, sport = out_map[(src, sport)]
        if dst in subs:
            if dport != "image" or not subs[dst]["inputs"]:
                raise SubgraphError(dst, f"No input '{dport}'."
                                         + (" Inputs: image." if subs[dst]["inputs"] else
                                            " This pipeline takes no input."))
            feeds[dst] = [src, sport]
            continue
        edges_out.append({"from": [src, sport], "to": [dst, dport]})
    for sid, itf in subs.items():
        if itf["inputs"] and sid not in feeds:
            raise SubgraphError(sid, "Required input 'image' is not connected.")
        for e in itf["_graph"]["edges"]:
            (src, sport), (dst, dport) = e["from"], e["to"]
            new_src = feeds[sid] if src == INPUT_NODE_ID else [f"{sid}{SEP}{src}", sport]
            edges_out.append({"from": list(new_src), "to": [f"{sid}{SEP}{dst}", dport]})
    if len(nodes_out) > MAX_NODES:
        raise SubgraphError(None, f"Expanded pipeline has {len(nodes_out)} nodes (limit {MAX_NODES}).")
    out = {k: v for k, v in graph.items() if k not in ("nodes", "edges")}
    out.update({"nodes": nodes_out, "edges": edges_out})
    return out, extra


def expand(graph: dict, viewer_id: str, customs: dict | None = None) -> tuple[dict, dict, dict]:
    """(plain graph, custom nodes incl. inner snapshots, outer map) for running.

    outer map: {sub id: {outer output port: [inner node, port]}} to report
    values and previews of the inner nodes on the node the user placed.
    Raises SubgraphError."""
    customs = dict(customs or {})
    if not has_subs(graph):
        return graph, customs, {}
    outer: dict[str, dict] = {}
    plain, extra = _expand_graph(graph, viewer_id, customs, 1, outer)
    customs.update(extra)
    return plain, customs, outer


def top_id(node_id: Optional[str]) -> Optional[str]:
    return node_id.split(SEP, 1)[0] if node_id else node_id


def map_issues(issues: list[Issue], graph: dict) -> list[Issue]:
    """Inner-node issues reported on the node the user placed."""
    subs = {n["id"] for n in graph.get("nodes") or [] if is_sub(n)}
    out = []
    for i in issues:
        top = top_id(i.node_id)
        if i.node_id and top in subs and i.node_id != top:
            inner = i.node_id[len(top) + 1:]
            out.append(Issue(top, f"[{inner}] {i.message}", i.severity))
        else:
            out.append(i)
    return out


def validate(graph: dict, custom_nodes: dict | None, viewer_id: Optional[str]) -> list[Issue]:
    """runner.validate, with pipeline nodes expanded (and their issues mapped back)."""
    if not has_subs(graph):
        return runner.validate(graph, custom_nodes=custom_nodes)
    if not viewer_id:
        return [Issue(None, "Pipeline nodes need a signed-in user.")]
    try:
        plain, customs, _ = expand(graph, viewer_id, custom_nodes)
    except SubgraphError as e:
        return [Issue(e.node_id, str(e))]
    return map_issues(runner.validate(plain, custom_nodes=customs), graph)


def collapse(result: dict, outer: dict) -> None:
    """Report inner-node results on the pipeline node the user placed (D-045):
    its values/previews come from the inner nodes producing its outputs; a
    failure inside it is reported on it."""
    values = result.get("values") or {}
    previews = result.get("previews") or {}
    for sid, ports in outer.items():
        v = {port: values[inner][iport] for port, (inner, iport) in ports.items()
             if iport in (values.get(inner) or {})}
        if v:
            values[sid] = v
        for inner, _ in ports.values():
            if inner in previews:
                previews[sid] = previews[inner]
                break
    if values:
        result["values"] = values
    failed = result.get("failed_node")
    if failed and top_id(failed) in outer and failed != top_id(failed):
        result["failed_node"] = top_id(failed)
        result["error"] = f"[{failed.split('/', 1)[1]}] {result.get('error')}"
