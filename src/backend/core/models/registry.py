"""Declarative registry of VRAM-consuming models.

A model is *declared* here, never loaded here. `@register_model` stores a loader
callable and a VRAM estimate; nothing touches the GPU until ModelManager decides
to load it in response to an actual request.

This module (and this package) is the ONLY sanctioned place where model weights
may be loaded — `guard.py` enforces that by rejecting direct loading calls in op
modules. To add a model see CODING_STYLE.md "Adding a model".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ModelSpec:
    name: str
    loader: Callable[[str], object]   # loader(device) -> model handle
    vram_mb: int                      # estimate used for the budget/eviction math
    doc: str = ""
    unloader: Optional[Callable[[object], None]] = None  # optional extra teardown

    def to_dict(self) -> dict:
        return {"name": self.name, "vram_mb": self.vram_mb, "doc": self.doc}


_MODELS: dict[str, ModelSpec] = {}


def register_model(name: str, vram_mb: int, doc: str = "",
                   unloader: Optional[Callable[[object], None]] = None):
    """Decorate a `loader(device) -> handle` function to declare a model.

    The decorated function is stored, NOT called — importing this module must
    stay free of GPU work so the app can boot without touching VRAM.
    """
    def decorator(loader: Callable[[str], object]) -> Callable[[str], object]:
        if name in _MODELS:
            raise ValueError(f"Duplicate model name: {name}")
        _MODELS[name] = ModelSpec(name=name, loader=loader, vram_mb=vram_mb,
                                  doc=doc, unloader=unloader)
        return loader
    return decorator


def get_model_spec(name: str) -> Optional[ModelSpec]:
    _ensure_loaded()
    return _MODELS.get(name)


def all_models() -> dict[str, ModelSpec]:
    _ensure_loaded()
    return dict(_MODELS)


def catalog() -> list[dict]:
    _ensure_loaded()
    return [s.to_dict() for s in _MODELS.values()]


_loaded = False
_load_lock = threading.Lock()


def _ensure_loaded() -> None:
    """Import model definition modules once so their decorators run.

    Same threading caution as pipeline.registry: the flag flips only after the
    import finishes, under a lock, so a concurrent caller can't observe an
    empty registry mid-import.
    """
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        from . import definitions  # noqa: F401
        _loaded = True
