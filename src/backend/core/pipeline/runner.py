"""Pipeline DAG validation + execution.

Graph JSON shape:
    {
      "nodes": [{"id": "n1", "op": "otsu_threshold", "params": {...}},
                {"id": "n2", "custom_node_id": "abc...", "params": {...}}],
      "edges": [{"from": ["n1", "mask"], "to": ["n2", "mask"]}]
    }

The special source node id "input" is implicit: edges may originate from
["input", "image"] to feed the session's image into the graph.

Trust model: registry ops are our own trusted code, so the runner does NOT
apply the sandbox's import whitelist. Custom nodes are user/LLM code and are
AST-checked at *save* time (custom_nodes store) using the same checker as
run-code. Execution still happens in a resource-limited subprocess (see
executor.py's rlimit/BLAS pattern, D-013) so a runaway op can't take the
server down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from . import registry
from .types import PortType, apply_coercion, can_connect

INPUT_NODE_ID = "input"


@dataclass
class Issue:
    node_id: Optional[str]
    message: str
    severity: str = "error"

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "message": self.message, "severity": self.severity}


@dataclass
class NodeResult:
    node_id: str
    outputs: dict[str, np.ndarray] = field(default_factory=dict)
    error: Optional[str] = None


def _node_ports(node: dict, custom_nodes: dict[str, dict]) -> tuple[list, list, list, str]:
    """Return (inputs, outputs, params, label) for a node, or raise KeyError."""
    if node.get("op"):
        spec = registry.get_op(node["op"])
        if spec is None:
            raise KeyError(node["op"])
        return spec.inputs, spec.outputs, spec.params, spec.name
    cn_id = node.get("custom_node_id")
    cn = custom_nodes.get(cn_id)
    if cn is None:
        raise KeyError(cn_id or "<missing custom_node_id>")
    from .registry import Param, Port
    ins = [Port(p["name"], PortType(p["type"])) for p in cn["ports"]["inputs"]]
    outs = [Port(p["name"], PortType(p["type"])) for p in cn["ports"]["outputs"]]
    params = [Param(p["name"], p.get("kind", "float"), p.get("default")) for p in cn.get("params", [])]
    return ins, outs, params, f"custom:{cn['name']}"


def validate(graph: dict, custom_nodes: dict[str, dict] | None = None) -> list[Issue]:
    """Full static validation. Returns [] when the graph is runnable."""
    custom_nodes = custom_nodes or {}
    issues: list[Issue] = []

    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    if not nodes:
        return [Issue(None, "Pipeline has no nodes.")]

    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids):
        issues.append(Issue(None, "Duplicate node ids in graph."))
    if INPUT_NODE_ID in ids:
        issues.append(Issue(None, f"'{INPUT_NODE_ID}' is a reserved node id."))

    by_id: dict[str, dict] = {n["id"]: n for n in nodes if n.get("id")}
    ports: dict[str, tuple] = {}

    for node in nodes:
        nid = node.get("id")
        if not nid:
            issues.append(Issue(None, "Node missing 'id'."))
            continue
        try:
            ins, outs, params, _ = _node_ports(node, custom_nodes)
        except KeyError as e:
            issues.append(Issue(nid, f"Unknown op or custom node: {e.args[0]}"))
            continue
        ports[nid] = (ins, outs)

        # Param validation against the declared schema
        supplied = node.get("params") or {}
        known = {p.name: p for p in params}
        for k, v in supplied.items():
            if k not in known:
                issues.append(Issue(nid, f"Unknown param '{k}'."))
                continue
            err = known[k].validate(v)
            if err:
                issues.append(Issue(nid, err))

    # Edge validation (types + endpoints)
    incoming: dict[str, dict[str, tuple[str, str]]] = {nid: {} for nid in by_id}
    for e in edges:
        try:
            src_id, src_port = e["from"]
            dst_id, dst_port = e["to"]
        except (KeyError, ValueError, TypeError):
            issues.append(Issue(None, f"Malformed edge: {e!r}"))
            continue

        if dst_id not in by_id:
            issues.append(Issue(None, f"Edge targets unknown node '{dst_id}'."))
            continue
        if src_id != INPUT_NODE_ID and src_id not in by_id:
            issues.append(Issue(None, f"Edge originates from unknown node '{src_id}'."))
            continue
        if dst_id not in ports:
            continue  # node already errored

        dst_ins = {p.name: p for p in ports[dst_id][0]}
        if dst_port not in dst_ins:
            issues.append(Issue(dst_id, f"No input port '{dst_port}' on this node."))
            continue

        if src_id == INPUT_NODE_ID:
            # The session image is a wildcard SOURCE: its concrete type isn't
            # known until run time (grayscale vs RGB upload), so it may feed
            # any image-ish port. run() re-checks with infer_type() and either
            # coerces or fails that node with a clear message.
            allowed_from_input = {PortType.IMAGE_ANY, PortType.IMAGE_RGB,
                                  PortType.IMAGE_GRAY, PortType.FLOAT_MAP, PortType.ANY}
            if dst_ins[dst_port].type not in allowed_from_input:
                issues.append(Issue(
                    dst_id,
                    f"The source image cannot feed input '{dst_port}' "
                    f"({dst_ins[dst_port].type.value}) — it only provides an image."))
                continue
            incoming[dst_id][dst_port] = (src_id, src_port)
            continue
        else:
            if src_id not in ports:
                continue
            src_outs = {p.name: p for p in ports[src_id][1]}
            if src_port not in src_outs:
                issues.append(Issue(src_id, f"No output port '{src_port}' on this node."))
                continue
            src_type = src_outs[src_port].type

        ok, coercion = can_connect(src_type, dst_ins[dst_port].type)
        if not ok:
            hint = ""
            if src_type == PortType.IMAGE_RGB and dst_ins[dst_port].type == PortType.IMAGE_GRAY:
                hint = " Insert a 'to_grayscale' node between them."
            issues.append(Issue(
                dst_id,
                f"Type mismatch: {src_id}.{src_port} ({src_type.value}) -> "
                f"{dst_id}.{dst_port} ({dst_ins[dst_port].type.value}).{hint}"))
            continue

        if dst_port in incoming[dst_id]:
            issues.append(Issue(dst_id, f"Input port '{dst_port}' has more than one incoming edge."))
        incoming[dst_id][dst_port] = (src_id, src_port)

    # Required-input check
    for nid, (ins, _outs) in ports.items():
        for p in ins:
            if p.name not in incoming.get(nid, {}):
                issues.append(Issue(nid, f"Required input '{p.name}' is not connected."))

    # Cycle detection (Kahn topological sort over node-level dependencies)
    if not any(i.severity == "error" for i in issues):
        order = _topo_order(by_id, edges)
        if order is None:
            issues.append(Issue(None, "Pipeline contains a cycle."))

    return issues


def _topo_order(by_id: dict[str, dict], edges: list[dict]) -> Optional[list[str]]:
    deps: dict[str, set[str]] = {nid: set() for nid in by_id}
    for e in edges:
        try:
            src_id = e["from"][0]
            dst_id = e["to"][0]
        except Exception:
            continue
        if src_id != INPUT_NODE_ID and src_id in by_id and dst_id in by_id:
            deps[dst_id].add(src_id)

    order: list[str] = []
    ready = [nid for nid, d in deps.items() if not d]
    remaining = dict(deps)
    while ready:
        nid = ready.pop()
        order.append(nid)
        del remaining[nid]
        for other, d in remaining.items():
            if nid in d:
                d.discard(nid)
                if not d:
                    ready.append(other)
    return order if not remaining else None


def run(graph: dict, input_image: np.ndarray,
        custom_nodes: dict[str, dict] | None = None,
        on_node: Callable[[str, str, dict], None] | None = None) -> dict[str, Any]:
    """Execute a validated graph.

    on_node(node_id, phase, payload) is called with phase in
    {"start", "done", "error"} so callers can stream progress / write previews.

    Returns {"results": {node_id: {port: array}}, "terminal": [node_ids],
             "error": str|None, "failed_node": str|None}
    """
    custom_nodes = custom_nodes or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id = {n["id"]: n for n in nodes}

    order = _topo_order(by_id, edges)
    if order is None:
        return {"results": {}, "terminal": [], "error": "Pipeline contains a cycle.",
                "failed_node": None}

    incoming: dict[str, dict[str, tuple[str, str]]] = {nid: {} for nid in by_id}
    for e in edges:
        src_id, src_port = e["from"]
        dst_id, dst_port = e["to"]
        if dst_id in incoming:
            incoming[dst_id][dst_port] = (src_id, src_port)

    results: dict[str, dict[str, np.ndarray]] = {INPUT_NODE_ID: {"image": input_image}}
    compiled_custom: dict[str, Callable] = {}

    for nid in order:
        node = by_id[nid]
        if on_node:
            on_node(nid, "start", {})
        try:
            ins, outs, params, _label = _node_ports(node, custom_nodes)

            kwargs: dict[str, Any] = {}
            for p in ins:
                src_id, src_port = incoming[nid][p.name]
                value = results[src_id][src_port]
                from .types import infer_type
                ok, coercion = can_connect(infer_type(value), p.type)
                if coercion:
                    value = apply_coercion(coercion, value)
                kwargs[p.name] = value

            supplied = node.get("params") or {}
            for p in params:
                kwargs[p.name] = supplied.get(p.name, p.default)

            if node.get("op"):
                spec = registry.get_op(node["op"])
                if spec.requires_model:
                    # Model ops never execute here: the sidecar owns the
                    # model's lifetime (lazy load, idle unload, VRAM budget).
                    # This process only ships arrays across (see D-030).
                    from ..models.sidecar import run_model_op
                    port_names = {p.name for p in ins}
                    out = run_model_op(
                        spec.name,
                        inputs={k: v for k, v in kwargs.items() if k in port_names},
                        params={k: v for k, v in kwargs.items() if k not in port_names},
                    )
                else:
                    out = spec.fn(**kwargs)
            else:
                cn = custom_nodes[node["custom_node_id"]]
                fn = compiled_custom.get(node["custom_node_id"])
                if fn is None:
                    fn = _compile_custom_node(cn)
                    compiled_custom[node["custom_node_id"]] = fn
                node_inputs = {p.name: kwargs[p.name] for p in ins}
                node_params = {p.name: kwargs[p.name] for p in params}
                out = fn(node_inputs, node_params)

            if not isinstance(out, dict):
                raise TypeError(f"Node returned {type(out).__name__}, expected a dict of outputs.")
            missing = [p.name for p in outs if p.name not in out]
            if missing:
                raise KeyError(f"Node did not return declared output(s): {missing}")

            results[nid] = out
            if on_node:
                on_node(nid, "done", {"outputs": out})
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if on_node:
                on_node(nid, "error", {"message": msg})
            return {"results": results, "terminal": [], "error": msg, "failed_node": nid}

    # Terminal nodes = those whose outputs feed nothing else
    consumed = {e["from"][0] for e in edges}
    terminal = [nid for nid in order if nid not in consumed]

    return {"results": results, "terminal": terminal, "error": None, "failed_node": None}


def _compile_custom_node(cn: dict) -> Callable:
    """Exec a saved custom node's code and return its `node` callable.

    Safety comes from the AST check performed when the node was SAVED
    (custom_nodes store) — the same checker used for run-code. This exec
    happens inside the resource-limited pipeline subprocess.
    """
    namespace: dict[str, Any] = {}
    exec(compile(cn["code"], f"<custom_node:{cn['name']}>", "exec"), namespace)
    fn = namespace.get("node")
    if not callable(fn):
        raise ValueError(f"Custom node '{cn['name']}' does not define node(inputs, params).")
    return fn
