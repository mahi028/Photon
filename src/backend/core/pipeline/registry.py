"""Op registry: curated, trusted image-processing operations for pipelines.

Ops are thin wrappers over skimage/cv2/scipy — expose, don't rewrite. Each op
declares typed input/output ports and a param schema; the registry serves both
the editor palette (catalog()) and the LLM system prompt (catalog_for_prompt())
from the same source, so neither can drift.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .types import PortType, coercion_table


@dataclass
class Port:
    name: str
    type: PortType
    optional: bool = False       # input may stay unconnected (library ops)

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.type.value}
        if self.optional:
            d["optional"] = True
        return d


@dataclass
class Param:
    name: str
    kind: str                    # "int" | "float" | "bool" | "choice" | "str" | "json"
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[list] = None
    doc: str = ""
    nullable: bool = False       # None is accepted (library "x=None" defaults)
    required: bool = False       # no default: must be supplied

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "default": self.default, "doc": self.doc}
        if self.nullable:
            d["nullable"] = True
        if self.required:
            d["required"] = True
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.choices is not None:
            d["choices"] = self.choices
        return d

    def validate(self, value: Any) -> Optional[str]:
        """Return an error string, or None if the value is acceptable."""
        if self.kind == "json":
            return None                  # any JSON value; the function checks it
        if value is None and self.nullable:
            return None
        if self.kind == "str":
            if not isinstance(value, str):
                return f"param '{self.name}' must be a string"
            return None
        if self.kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return f"param '{self.name}' must be an integer"
        elif self.kind == "float":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return f"param '{self.name}' must be a number"
        elif self.kind == "bool":
            if not isinstance(value, bool):
                return f"param '{self.name}' must be a boolean"
        elif self.kind == "choice":
            if value not in (self.choices or []):
                return f"param '{self.name}' must be one of {self.choices}"
        if self.kind in ("int", "float"):
            if self.min is not None and value < self.min:
                return f"param '{self.name}' must be >= {self.min}"
            if self.max is not None and value > self.max:
                return f"param '{self.name}' must be <= {self.max}"
        return None


@dataclass
class OpSpec:
    name: str
    category: str
    fn: Callable
    inputs: list[Port]
    outputs: list[Port]
    params: list[Param] = field(default_factory=list)
    doc: str = ""
    # Set by @register_model_op: the registered model this op needs. Ops with
    # this set are dispatched to the model sidecar (which owns the model's
    # lifetime) instead of running in the sandboxed pipeline child.
    requires_model: Optional[str] = None
    # Library ops (library.py): only params the node sets are passed, so the
    # function's own defaults apply; `library` = {module, function, package}.
    pass_defaults: bool = True
    library: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "category": self.category,
            "inputs": [p.to_dict() for p in self.inputs],
            "outputs": [p.to_dict() for p in self.outputs],
            "params": [p.to_dict() for p in self.params],
            "doc": self.doc,
            "requires_model": self.requires_model,
        }
        if self.library:
            d["library"] = self.library
        return d


_OPS: dict[str, OpSpec] = {}


def register(name: str, category: str, inputs: list[Port], outputs: list[Port],
             params: list[Param] | None = None, doc: str = ""):
    """Decorator registering an op implementation into the global registry."""
    def decorator(fn: Callable) -> Callable:
        if name in _OPS:
            raise ValueError(f"Duplicate op name: {name}")
        _OPS[name] = OpSpec(
            name=name, category=category, fn=fn,
            inputs=inputs, outputs=outputs, params=params or [], doc=doc,
        )
        return fn
    return decorator


def register_model_op(name: str, category: str, model: str, inputs: list[Port],
                      outputs: list[Port], params: list[Param] | None = None,
                      doc: str = ""):
    """Register an op that needs a GPU/VRAM model.

    The decorated function receives an already-loaded handle as its `model`
    keyword argument — it must NOT load anything itself (guard.py enforces
    this). ModelManager, running in the sidecar, owns the model's lifetime:
    lazy load on first request, refcounted while in use, unloaded after the
    idle timeout, LRU-evicted under the VRAM budget.

        @register_model_op(name="dinov2_features", category="features",
                           model="dinov2_vitb14", inputs=[...], outputs=[...])
        def dinov2_features(image, model, layer=-1): ...
    """
    def decorator(fn: Callable) -> Callable:
        if name in _OPS:
            raise ValueError(f"Duplicate op name: {name}")
        _OPS[name] = OpSpec(
            name=name, category=category, fn=fn,
            inputs=inputs, outputs=outputs, params=params or [], doc=doc,
            requires_model=model,
        )
        return fn
    return decorator


def get_op(name: str) -> Optional[OpSpec]:
    if name.startswith("lib:"):
        from . import library
        return library.get(name)
    _ensure_loaded()
    return _OPS.get(name)


def all_ops() -> dict[str, OpSpec]:
    _ensure_loaded()
    return dict(_OPS)


def catalog(include_library: bool = False) -> dict:
    """Full JSON for the editor palette + client-side type checking.
    include_library adds every library op (library.py, several hundred)."""
    _ensure_loaded()
    ops = [spec.to_dict() for spec in _OPS.values()]
    if include_library:
        from . import library
        ops += [spec.to_dict() for spec in library.all_specs().values()]
    return {
        "ops": ops,
        "port_types": [t.value for t in PortType],
        "coercions": coercion_table(),
    }


def catalog_for_prompt(custom_nodes: list[dict] | None = None) -> str:
    """Compact one-line-per-op text for the LLM system prompt.

    custom_nodes: the requesting user's saved custom nodes ({name, ports_json...})
    so the LLM retrieves existing custom work instead of regenerating it.
    """
    _ensure_loaded()
    lines = []
    by_cat: dict[str, list[OpSpec]] = {}
    for spec in _OPS.values():
        by_cat.setdefault(spec.category, []).append(spec)
    for cat in sorted(by_cat):
        for spec in sorted(by_cat[cat], key=lambda s: s.name):
            ins = ", ".join(f"{p.name}:{p.type.value}" for p in spec.inputs)
            outs = ", ".join(f"{p.name}:{p.type.value}" for p in spec.outputs)
            ps = ", ".join(
                f"{p.name}={p.default}" for p in spec.params
            )
            ps = f" params({ps})" if ps else ""
            tag = f" [model:{spec.requires_model}]" if spec.requires_model else ""
            lines.append(f"- {spec.name} [{cat}]{tag} ({ins}) -> ({outs}){ps} — {spec.doc}")
    if custom_nodes:
        lines.append("Your saved custom nodes (reuse instead of regenerating):")
        for cn in custom_nodes:
            lines.append(f"- custom:{cn['name']} ports={cn.get('ports_json', '?')}")
    return "\n".join(lines)


_loaded = False
_load_lock = threading.Lock()


def _ensure_loaded() -> None:
    """Import all op modules exactly once so their @register decorators run.

    The model-policy guard runs FIRST: if an op module loads model weights
    directly (bypassing ModelManager's lazy-load/idle-unload/VRAM budget), we
    refuse to start rather than silently pin VRAM forever. See models/guard.py.

    Thread-safety matters here: the model sidecar serves each connection on its
    own thread, so concurrent first-requests race. The `_loaded` flag is set
    only AFTER the imports complete, under a lock — setting it first would let
    a second thread skip the import and then find an empty registry.
    """
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        from ..models.guard import check_op_modules
        check_op_modules()
        from .ops import (color, threshold, morphology, filters, features,  # noqa: F401
                          masking, geometry, models, glue)
        _loaded = True
