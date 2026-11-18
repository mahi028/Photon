"""Glue ops (D-041): the small numpy steps that connect library functions.

Library functions return thresholds, tuples, label arrays, tables and
numbers, not just images. These ops turn those into something the next node
takes: pick an element, digitize by thresholds, arithmetic, compare, cast,
pick a channel, stack, reduce to a number. A curated numpy subset instead of
exposing numpy itself (too many footguns: memory-mapping, pickle loading,
object arrays, global state).
"""

from __future__ import annotations

import numpy as np

from ..registry import Param, Port, register
from ..types import PortType

_A = PortType.ARRAY

_UNARY = {"abs": np.abs, "negative": np.negative, "sqrt": np.sqrt, "log1p": np.log1p,
          "exp": np.exp, "square": np.square}
_BINARY = {"add": np.add, "subtract": np.subtract, "multiply": np.multiply,
           "divide": np.true_divide, "power": np.power, "minimum": np.minimum,
           "maximum": np.maximum}
_COMPARE = {">": np.greater, ">=": np.greater_equal, "<": np.less, "<=": np.less_equal,
            "==": np.equal, "!=": np.not_equal}
_DTYPES = ["uint8", "uint16", "int32", "int64", "float32", "float64", "bool"]


@register(
    name="get_item", category="glue",
    inputs=[Port("value", PortType.ANY)],
    outputs=[Port("item", PortType.ANY)],
    params=[Param("index", "json", 0,
                  doc="Position in a tuple/list/array (negative counts from the end), or a key of a dict.")],
    doc="Pick one element: e.g. the labels from (labels, num), or a column of a regionprops table.",
)
def get_item(value, index=0):
    if isinstance(value, dict):
        return {"item": value[index]}
    if isinstance(index, list):
        index = tuple(index)
    return {"item": value[index]}


@register(
    name="digitize", category="glue",
    inputs=[Port("image", _A), Port("bins", _A)],
    outputs=[Port("labels", _A)],
    doc="Label each pixel by which bin it falls in (np.digitize). With "
        "threshold_multiotsu: 0..classes-1 region labels.",
)
def digitize(image, bins):
    return {"labels": np.digitize(np.asarray(image), np.ravel(bins)).astype(np.int32)}


@register(
    name="math", category="glue",
    inputs=[Port("a", _A), Port("b", _A, optional=True)],
    outputs=[Port("out", _A)],
    params=[Param("op", "choice", "add", choices=list(_BINARY) + list(_UNARY),
                  doc="Binary ops use b (if connected) or value; unary ops ignore both."),
            Param("value", "float", 0.0, doc="Second operand when b is not connected.")],
    doc="Elementwise arithmetic on arrays (results are float/int; cast to view).",
)
def math(a, b=None, op="add", value=0.0):
    a = np.asarray(a)
    if op in _UNARY:
        return {"out": _UNARY[op](a.astype(np.float64) if op in ("sqrt", "log1p", "exp") else a)}
    other = np.asarray(b) if b is not None else value
    if a.dtype.kind in "ub" or (isinstance(other, np.ndarray) and other.dtype.kind in "ub"):
        a = a.astype(np.float64)        # no uint8 wrap-around
    return {"out": _BINARY[op](a, other)}


@register(
    name="compare", category="glue",
    inputs=[Port("a", _A), Port("b", _A, optional=True)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("op", "choice", ">", choices=list(_COMPARE)),
            Param("value", "float", 0.0, doc="Compared against when b is not connected.")],
    doc="Elementwise comparison to a mask, e.g. labels == 2, or image > b.",
)
def compare(a, b=None, op=">", value=0.0):
    other = np.asarray(b) if b is not None else value
    return {"mask": _COMPARE[op](np.asarray(a), other)}


@register(
    name="astype", category="glue",
    inputs=[Port("array", _A)],
    outputs=[Port("array", _A)],
    params=[Param("dtype", "choice", "uint8", choices=_DTYPES),
            Param("scale", "choice", "clip", choices=["clip", "minmax", "none"],
                  doc="For integer targets: clip to the range, stretch min..max to it, or cast as-is.")],
    doc="Change dtype, e.g. float64 to uint8 for OpenCV, with clipping or min-max scaling.",
)
def astype(array, dtype="uint8", scale="clip"):
    a = np.asarray(array)
    dt = np.dtype(dtype)
    if dt.kind in "ui" and scale != "none":
        info = np.iinfo(dt)
        f = a.astype(np.float64)
        if scale == "minmax":
            lo, hi = float(np.nanmin(f)), float(np.nanmax(f))
            f = (f - lo) / (hi - lo) * info.max if hi > lo else np.zeros_like(f)
        a = np.clip(np.nan_to_num(f), info.min, info.max)
    return {"array": a.astype(dt)}


@register(
    name="take", category="glue",
    inputs=[Port("array", _A)],
    outputs=[Port("array", _A)],
    params=[Param("index", "int", 0), Param("axis", "int", -1, doc="-1 = channels.")],
    doc="One slice along an axis: a channel of an RGB/feature image, or a plane of a stack.",
)
def take(array, index=0, axis=-1):
    return {"array": np.take(np.asarray(array), index, axis=axis)}


@register(
    name="stack", category="glue",
    inputs=[Port("a", _A), Port("b", _A), Port("c", _A, optional=True)],
    outputs=[Port("array", _A)],
    params=[Param("axis", "int", -1, doc="-1 stacks as channels (e.g. three grays to RGB).")],
    doc="Stack 2 or 3 same-shape arrays along a new axis.",
)
def stack(a, b, c=None, axis=-1):
    parts = [np.asarray(a), np.asarray(b)] + ([np.asarray(c)] if c is not None else [])
    return {"array": np.stack(parts, axis=axis)}


@register(
    name="to_number", category="glue",
    inputs=[Port("value", PortType.ANY)],
    outputs=[Port("number", PortType.NUMBER)],
    params=[Param("reduce", "choice", "item",
                  choices=["item", "mean", "sum", "min", "max", "count_nonzero", "size"])],
    doc="Reduce a value to one number (an array's mean, a count of True pixels...).",
)
def to_number(value, reduce="item"):
    if reduce == "item":
        a = np.asarray(value)
        return {"number": a.item() if a.size == 1 else float("nan")}
    a = np.asarray(value)
    if reduce == "size":
        return {"number": int(a.size)}
    if reduce == "count_nonzero":
        return {"number": int(np.count_nonzero(a))}
    return {"number": float(getattr(np, reduce)(a))}
