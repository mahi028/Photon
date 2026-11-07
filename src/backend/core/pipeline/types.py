"""Port type system for pipeline node/edge compatibility (v3 — see photon_v3_plan.md).

The server is the source of truth for what may connect to what;
the editor mirrors COERCIONS client-side (shipped in GET /api/ops) for
instant feedback, but runner.validate() always re-checks.
"""

from __future__ import annotations

from enum import Enum

import numpy as np


class PortType(str, Enum):
    IMAGE_GRAY = "IMAGE_GRAY"      # (H, W) uint8 or float in [0,1]
    IMAGE_RGB = "IMAGE_RGB"        # (H, W, 3) uint8
    IMAGE_ANY = "IMAGE_ANY"        # either of the above
    MASK = "MASK"                  # (H, W) bool
    FLOAT_MAP = "FLOAT_MAP"        # (H, W) float, unnormalized (e.g. optical density)
    FEATURE_MAP = "FEATURE_MAP"    # (H, W, C) float — model/descriptor outputs (MIND now, DINOv2 in P4)
    NUMBER = "NUMBER"              # scalar
    ARRAY = "ARRAY"                # any ndarray (library ops, D-041)
    ANY = "ANY"                    # wildcard: any value (tuples, dicts, objects...)


# Every array-valued type. ARRAY connects to and from all of them; the
# concrete type is checked again at run time (infer_type + coercion).
ARRAY_TYPES = frozenset({PortType.IMAGE_GRAY, PortType.IMAGE_RGB, PortType.IMAGE_ANY,
                         PortType.MASK, PortType.FLOAT_MAP, PortType.FEATURE_MAP,
                         PortType.ARRAY})


# src -> dst pairs that connect WITHOUT coercion (beyond exact match).
# IMAGE_ANY is a wildcard on BOTH sides: pass-through ops (blur, gamma,
# crop...) preserve whatever they were given, so their declared IMAGE_ANY
# output may feed a concrete image port. The concrete type is re-checked at
# run time via infer_type(), which coerces or fails with a clear message.
_DIRECT: frozenset[tuple[PortType, PortType]] = frozenset({
    (PortType.IMAGE_GRAY, PortType.IMAGE_ANY),
    (PortType.IMAGE_RGB, PortType.IMAGE_ANY),
    (PortType.IMAGE_ANY, PortType.IMAGE_GRAY),
    (PortType.IMAGE_ANY, PortType.IMAGE_RGB),
    (PortType.IMAGE_ANY, PortType.FLOAT_MAP),
})

# src -> dst pairs that connect via an implicit, lossless-enough coercion.
# Deliberately absent: IMAGE_RGB -> IMAGE_GRAY. Dropping channels is a real
# decision — the user (or LLM) must insert an explicit to_grayscale node.
_COERCIONS: dict[tuple[PortType, PortType], str] = {
    (PortType.MASK, PortType.IMAGE_GRAY): "mask_to_gray",        # bool -> uint8*255
    (PortType.MASK, PortType.IMAGE_ANY): "mask_to_gray",
    (PortType.FLOAT_MAP, PortType.IMAGE_GRAY): "minmax_to_gray",  # min-max -> uint8
    (PortType.FLOAT_MAP, PortType.IMAGE_ANY): "minmax_to_gray",
    (PortType.IMAGE_GRAY, PortType.FLOAT_MAP): "gray_to_float",   # uint8 -> float [0,1]
}


def can_connect(src: PortType, dst: PortType) -> tuple[bool, str | None]:
    """Return (ok, coercion_name). coercion_name is None for direct matches."""
    if dst == PortType.ANY or src == dst:
        return True, None
    # Library wildcards (D-041): ANY output may feed anything; ARRAY pairs
    # with every array type, NUMBER may feed ARRAY (0-d). Checked at run time.
    if src == PortType.ANY:
        return True, None
    if (src == PortType.ARRAY and dst in ARRAY_TYPES) or \
       (dst == PortType.ARRAY and (src in ARRAY_TYPES or src == PortType.NUMBER)):
        return True, None
    if (src, dst) in _DIRECT:
        return True, None
    coercion = _COERCIONS.get((src, dst))
    if coercion:
        return True, coercion
    return False, None


def coercion_table() -> list[dict]:
    """Serializable table for the editor's client-side mirror."""
    rows = [{"src": s.value, "dst": d.value, "coercion": None} for s, d in _DIRECT]
    rows += [{"src": PortType.ANY.value, "dst": d.value, "coercion": None}
             for d in PortType if d != PortType.ANY]
    rows += [{"src": PortType.ARRAY.value, "dst": d.value, "coercion": None}
             for d in ARRAY_TYPES if d != PortType.ARRAY]
    rows += [{"src": s.value, "dst": PortType.ARRAY.value, "coercion": None}
             for s in (ARRAY_TYPES | {PortType.NUMBER}) if s != PortType.ARRAY]
    rows += [{"src": s.value, "dst": d.value, "coercion": name}
             for (s, d), name in _COERCIONS.items()]
    return rows


def apply_coercion(name: str, value: np.ndarray) -> np.ndarray:
    if name == "mask_to_gray":
        return (value.astype(np.uint8)) * 255
    if name == "minmax_to_gray":
        v = value.astype(np.float64)
        lo, hi = float(v.min()), float(v.max())
        if hi - lo < 1e-12:
            return np.zeros(v.shape, dtype=np.uint8)
        return ((v - lo) / (hi - lo) * 255.0).astype(np.uint8)
    if name == "gray_to_float":
        v = value.astype(np.float64)
        return v / 255.0 if v.max() > 1.0 else v
    raise ValueError(f"Unknown coercion: {name}")


def infer_type(value: np.ndarray) -> PortType:
    """Best-effort runtime type of an array (used for runtime sanity checks)."""
    if value.ndim == 2:
        if value.dtype == bool:
            return PortType.MASK
        if np.issubdtype(value.dtype, np.floating) and (value.max(initial=0) > 1.0 or value.min(initial=0) < 0.0):
            return PortType.FLOAT_MAP
        return PortType.IMAGE_GRAY
    if value.ndim == 3 and value.shape[2] == 3 and value.dtype == np.uint8:
        return PortType.IMAGE_RGB
    return PortType.FEATURE_MAP
