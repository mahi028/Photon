"""Library nodes (D-041): functions of allow-listed libraries as pipeline ops.

Instead of hand-wrapping every function, a node can name one directly:

    {"id": "t", "op": "lib:skimage.filters.threshold_multiotsu", "params": {"classes": 3}}

The op spec (ports, params, defaults, docs) is generated from the function
itself:

  * Python functions (skimage, scipy.ndimage): inspect.signature + the
    numpydoc "Parameters"/"Returns" sections. Array-typed arguments become
    input ports (optional when they have a default), scalar/str/tuple
    arguments become params, each documented return value becomes an output
    port.
  * OpenCV builtins (no Python signature): the "f(a, b[, c]) -> x, y" line
    of their docstring. Image-like argument names become ports; an optional
    argument that is also returned (dst, edges...) is an output buffer and is
    hidden; the rest are JSON params. Int-constant params accept names
    ("THRESH_BINARY|THRESH_OTSU").

Security: this module never resolves an arbitrary dotted path. A name is
accepted only if its module is in MODULES and the attribute is a public
function of that module, and it passes the deny rules (file / network /
GUI / pickle / callable-argument functions). Params are JSON, so no callables
or objects can be smuggled in. Execution still happens in the sandboxed
pipeline child (rlimits, audit-hook guard, optionally the gVisor worker):
the allow-list is about usefulness and footguns, the sandbox is the boundary.

Library ops get all-ARRAY/ANY port types (types.py): a wildcard that
connects to the typed image ports and is checked again at run time.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import json
import re
import threading
import warnings
from typing import Any, Optional

from .registry import OpSpec, Param, Port
from .types import PortType

PREFIX = "lib:"

# module -> distribution (for version pinning in published snapshots)
MODULES: dict[str, str] = {
    "skimage.color": "scikit-image",
    "skimage.exposure": "scikit-image",
    "skimage.feature": "scikit-image",
    "skimage.filters": "scikit-image",
    "skimage.filters.rank": "scikit-image",
    "skimage.measure": "scikit-image",
    "skimage.morphology": "scikit-image",
    "skimage.restoration": "scikit-image",
    "skimage.segmentation": "scikit-image",
    "skimage.transform": "scikit-image",
    "skimage.util": "scikit-image",
    "skimage.registration": "scikit-image",
    "skimage.metrics": "scikit-image",
    "skimage.draw": "scikit-image",
    "scipy.ndimage": "scipy",
    "cv2": "opencv-python-headless",
}

# Exact names that are never exposed (I/O, GUI, global state, or useless here).
DENY_FUNCS = frozenset({
    "cv2.imwrite", "cv2.imwritemulti", "cv2.imwriteWithMetadata", "cv2.imencode",
    "cv2.imencodeWithMetadata", "cv2.selectROI", "cv2.selectROIs", "cv2.addText",
    "skimage.util.apply_parallel", "skimage.util.lookfor", "skimage.measure.ransac",
})
# Any function whose name contains one of these words is excluded.
DENY_NAME_PARTS = ("read", "write", "load", "save", "show", "fetch", "download",
                   "plot", "view", "display", "pickle")
# A function with an argument of this name works with files: excluded.
FILE_PARAMS = frozenset({"fname", "filename", "file", "path", "filepath", "url", "uri"})
# Output buffers: never passed, the function allocates its own result.
OUTPUT_PARAMS = frozenset({"out", "output"})
# Undocumented arguments with these names are arrays (ports).
ARRAY_NAMES = frozenset({
    "image", "img", "im", "input", "ar", "arr", "array", "label_image", "labels",
    "mask", "markers", "image0", "image1", "im1", "im2", "image_true", "image_test",
    "reference_image", "moving_image", "intensity_image", "footprint", "selem",
    "kernel", "psf", "src", "src1", "src2", "src3", "templ", "prev", "next",
    "prevImg", "nextImg",
})
_CV2_PORTS = frozenset({"src", "src1", "src2", "src3", "image", "img", "mask",
                        "markers", "templ", "prev", "next", "prevImg", "nextImg"})
_CV2_ARRAY_OUTS = frozenset({"dst", "edges", "labels", "img", "image", "mask", "markers",
                             "stats", "centroids", "magnitude", "angle", "result"})

_ARRAY_RE = re.compile(r"\b(nd)?array(s|_like|-like)?\b|\bimage\b")
_DOC_MAX = 240


class LibraryError(Exception):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _jsonable(v: Any) -> tuple[bool, Any]:
    if hasattr(v, "item") and callable(v.item) and getattr(v, "shape", None) == ():
        v = v.item()                               # numpy scalar
    if isinstance(v, tuple):
        v = list(v)
    try:
        json.dumps(v)
        return True, v
    except (TypeError, ValueError):
        return False, None


def _alts(type_str: str) -> list[str]:
    """'int or tuple of int, optional' -> ['int', 'tuple of int']."""
    t = re.sub(r"\(default[^)]*\)", "", type_str.lower())
    t = re.sub(r"\{[^}]*\}", "{choices}", t)
    t = re.sub(r"\([^()]*\)|\[[^\[\]]*\]", " ", t)       # shapes: (M, N[, ...])
    t = re.sub(r"\([^()]*\)", " ", t)
    parts = re.split(r",|\bor\b|\|", t)
    out = []
    for p in parts:
        p = p.strip()
        if p and p not in ("optional", "none", "default"):
            out.append(p)
    return out


def _kind_for(type_str: str, default: Any, has_default: bool) -> tuple[str, Optional[list]]:
    choices = None
    if "{" in type_str:
        found = re.findall(r"'([^']*)'|\"([^\"]*)\"", type_str[type_str.index("{"):])
        vals = [a or b for a, b in found]
        if vals:
            choices = vals
            return "choice", choices
    if isinstance(default, bool):
        return "bool", None
    alts = _alts(type_str)
    if len(alts) == 1:
        a = alts[0]
        if a.startswith("bool"):
            return "bool", None
        if a.startswith("int"):
            return "int", None
        if a.startswith(("float", "scalar", "number")):
            return "float", None
        if a.startswith(("str", "string")):
            return "str", None
    if not alts and has_default:
        if isinstance(default, int):
            return "int", None
        if isinstance(default, float):
            return "float", None
        if isinstance(default, str):
            return "str", None
    return "json", None


def _short(text: str, n: int = _DOC_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _return_type(type_str: str) -> PortType:
    t = type_str.lower()
    if _ARRAY_RE.search(t):
        return PortType.ARRAY
    a = _alts(t)
    if a and a[0].startswith(("float", "int", "scalar", "bool", "number")):
        return PortType.NUMBER
    return PortType.ANY


def _denied(full: str, fn_name: str) -> bool:
    low = fn_name.lower()
    return full in DENY_FUNCS or any(w in low for w in DENY_NAME_PARTS)


def _caller(module: str, fn_name: str, out_names: list[str], is_cv2: bool):
    def call(**kw):
        mod = importlib.import_module(module)
        fn = getattr(mod, fn_name)
        if is_cv2:
            kw = {k: _cv2_value(mod, v) for k, v in kw.items()}
        else:
            kw = {k: (tuple(v) if isinstance(v, list) and all(not isinstance(x, (list, dict)) for x in v) else v)
                  for k, v in kw.items()}
        res = fn(**kw)
        if len(out_names) == 1:
            return {out_names[0]: res}
        if isinstance(res, tuple):
            vals = list(res) + [None] * (len(out_names) - len(res))
            return dict(zip(out_names, vals))
        return {out_names[0]: res, **{n: None for n in out_names[1:]}}
    return call


def _cv2_value(cv2, v):
    """JSON -> what OpenCV expects: constant names -> ints, flat lists ->
    tuples (ksize, anchor...), nested lists -> float32 arrays (kernels)."""
    if isinstance(v, str):
        parts = [p.strip().removeprefix("cv2.") for p in v.split("|")]
        if parts and all(p.isidentifier() and isinstance(getattr(cv2, p, None), int) for p in parts):
            out = 0
            for p in parts:
                out |= getattr(cv2, p)
            return out
        return v
    if isinstance(v, list):
        if v and all(isinstance(x, list) for x in v):
            import numpy as np
            return np.asarray(v, dtype=np.float32)
        return tuple(v)
    return v


# ---------------------------------------------------------------------------
# spec builders
# ---------------------------------------------------------------------------

def _python_spec(module: str, fn_name: str, fn) -> Optional[OpSpec]:
    full = f"{module}.{fn_name}"
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    doc_types: dict[str, str] = {}
    doc_desc: dict[str, str] = {}
    returns: list[tuple[str, str]] = []
    summary = ""
    try:
        from scipy._lib._docscrape import NumpyDocString
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            d = NumpyDocString(inspect.getdoc(fn) or "")
        for p in d["Parameters"]:
            for name in [n.strip() for n in p.name.split(",")]:
                doc_types[name] = p.type
                doc_desc[name] = " ".join(p.desc)
        for r in d["Returns"]:
            names = [n.strip() for n in r.name.split(",")] if r.name else [""]
            for name in names:
                returns.append((name, r.type))
        summary = " ".join(d["Summary"] + d["Extended Summary"][:1])
    except Exception:
        summary = (inspect.getdoc(fn) or "").split("\n\n")[0]

    inputs: list[Port] = []
    params: list[Param] = []
    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if name in FILE_PARAMS:
            return None
        has_default = p.default is not inspect.Parameter.empty
        if name in OUTPUT_PARAMS:
            if not has_default:
                return None
            continue
        t = doc_types.get(name, "")
        tl = t.lower()
        first = (_alts(t) or [""])[0]
        if "callable" in tl or first.startswith(("function", "callable")):
            if not has_default:
                return None
            continue
        alts = _alts(t)
        arrayish = [bool(_ARRAY_RE.search(a)) and not a.startswith(("dtype", "data-type")) for a in alts]
        if (arrayish and all(arrayish)) or (not t and name in ARRAY_NAMES):
            inputs.append(Port(name, PortType.ARRAY, optional=has_default))
            continue
        if any(arrayish):
            # "int or ndarray": an optional port AND a param of the same name;
            # a connected port wins (runner), else the param is passed.
            inputs.append(Port(name, PortType.ARRAY, optional=True))
        ok, default = _jsonable(p.default) if has_default else (True, None)
        kind, choices = _kind_for(t, default if ok else None, has_default and ok)
        lib_default = not ok
        params.append(Param(
            name, kind, default if ok else None, choices=choices,
            doc=_short(f"{t}. {doc_desc.get(name, '')}".strip(". ")) + (" (library default)" if lib_default else ""),
            nullable=has_default and (default is None or "none" in tl),
            required=not has_default))

    outputs: list[Port] = []
    seen = set()
    for name, t in returns:
        if not name.isidentifier() or not t:
            t, name = (t or name), "result"
        base, i = name, 2
        while name in seen:
            name, i = f"{base}{i}", i + 1
        seen.add(name)
        outputs.append(Port(name, _return_type(t)))
    if not outputs:
        outputs = [Port("result", PortType.ANY)]

    return OpSpec(
        name=PREFIX + full, category=module, fn=_caller(module, fn_name, [o.name for o in outputs], False),
        inputs=inputs, outputs=outputs, params=params, doc=_short(summary or fn_name),
        pass_defaults=False, library={"module": module, "function": fn_name,
                                      "package": MODULES[module]})


_CV2_SIG = re.compile(r"^\s*(\w+)\((.*)\)\s*->\s*(.*)$")


def _cv2_spec(fn_name: str, fn) -> Optional[OpSpec]:
    doc = fn.__doc__ or ""
    m = _CV2_SIG.match(doc.strip().splitlines()[0] if doc.strip() else "")
    if not m:
        return None
    arg_str, ret_str = m.group(2), m.group(3)
    required_part = arg_str.split("[")[0]
    required = [a.strip() for a in required_part.split(",") if a.strip()]
    optional = [a.strip() for a in re.sub(r"[\[\]]", "", arg_str[len(required_part):]).split(",") if a.strip()]
    rets = [r.strip() for r in ret_str.split(",") if r.strip() and r.strip() != "None"]
    if not any(a in _CV2_PORTS for a in required):
        return None                       # not an image function (global state, GUI...)
    if any(a in FILE_PARAMS for a in required + optional):
        return None
    pdoc = dict(re.findall(r"@param\s+(\w+)\s+([^\n]*)", doc))
    brief = re.search(r"@brief\s+([^\n]*(?:\n\.\s{3,}(?!@)[^\n]*)*)", doc)
    summary = re.sub(r"\n\.\s+", " ", brief.group(1)) if brief else fn_name

    inputs, params = [], []
    for a in required:
        if a in _CV2_PORTS:
            inputs.append(Port(a, PortType.ARRAY))
        else:
            params.append(Param(a, "json", None, doc=_short(pdoc.get(a, "")), required=True))
    for a in optional:
        if a in rets:
            continue                      # output buffer
        if a in _CV2_PORTS:
            inputs.append(Port(a, PortType.ARRAY, optional=True))
        else:
            params.append(Param(a, "json", None, doc=_short(pdoc.get(a, "")), nullable=True))
    outputs = [Port(r, PortType.NUMBER if r == "retval" else
                    PortType.ARRAY if r in _CV2_ARRAY_OUTS else PortType.ANY) for r in rets] \
        or [Port("result", PortType.ANY)]
    return OpSpec(
        name=f"{PREFIX}cv2.{fn_name}", category="cv2",
        fn=_caller("cv2", fn_name, [o.name for o in outputs], True),
        inputs=inputs, outputs=outputs, params=params,
        doc=_short(f"{doc.strip().splitlines()[0]}. {summary}"),
        pass_defaults=False, library={"module": "cv2", "function": fn_name,
                                      "package": MODULES["cv2"]})


def _public_names(mod) -> list[str]:
    names = getattr(mod, "__all__", None) or [n for n in dir(mod) if not n.startswith("_")]
    return [n for n in names if not n.startswith("_")]


def _build(module: str, fn_name: str) -> Optional[OpSpec]:
    if module not in MODULES or not fn_name.isidentifier() or fn_name.startswith("_"):
        return None
    if _denied(f"{module}.{fn_name}", fn_name):
        return None
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    if fn_name not in _public_names(mod):
        return None
    fn = getattr(mod, fn_name, None)
    if module == "cv2":
        if type(fn).__name__ != "builtin_function_or_method":
            return None
        return _cv2_spec(fn_name, fn)
    if not inspect.isfunction(fn):
        return None
    try:
        return _python_spec(module, fn_name, fn)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

_cache: dict[str, Optional[OpSpec]] = {}
_all: Optional[dict[str, OpSpec]] = None
_lock = threading.Lock()


def split_name(name: str) -> Optional[tuple[str, str]]:
    if not name.startswith(PREFIX):
        return None
    dotted = name[len(PREFIX):]
    for module in sorted(MODULES, key=len, reverse=True):   # longest first: filters.rank
        if dotted.startswith(module + ".") and "." not in dotted[len(module) + 1:]:
            return module, dotted[len(module) + 1:]
    return None


def get(name: str) -> Optional[OpSpec]:
    """Spec for one 'lib:module.function' name, built on demand (the pipeline
    child only imports the module it needs)."""
    if name in _cache:
        return _cache[name]
    parts = split_name(name)
    spec = _build(*parts) if parts else None
    with _lock:
        _cache[name] = spec
    return spec


def all_specs() -> dict[str, OpSpec]:
    """Every exposed library function (imports all modules once)."""
    global _all
    if _all is None:
        with _lock:
            if _all is None:
                out: dict[str, OpSpec] = {}
                for module in MODULES:
                    try:
                        mod = importlib.import_module(module)
                    except ImportError:
                        continue
                    for fn_name in _public_names(mod):
                        name = f"{PREFIX}{module}.{fn_name}"
                        spec = _cache[name] if name in _cache else _build(module, fn_name)
                        _cache[name] = spec
                        if spec:
                            out[name] = spec
                _all = out
    return _all


def search(query: str, limit: int = 40) -> list[OpSpec]:
    """Rank library specs by how well they match whitespace-separated terms
    (name hits weigh more than doc hits)."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    scored = []
    for spec in all_specs().values():
        name, doc = spec.name.lower(), spec.doc.lower()
        score = 0
        for t in terms:
            if t in name.rsplit(".", 1)[-1]:
                score += 3
            elif t in name:
                score += 2
            elif t in doc:
                score += 1
            else:
                score = -1
                break
        if score > 0:
            scored.append((score, spec.name))
    scored.sort(key=lambda s: (-s[0], len(s[1])))
    specs = all_specs()
    return [specs[n] for _, n in scored[:limit]]


def names_for_prompt() -> str:
    """Compact module -> function list for the LLM system prompt."""
    by_mod: dict[str, list[str]] = {}
    for spec in all_specs().values():
        by_mod.setdefault(spec.library["module"], []).append(spec.library["function"])
    return "\n".join(f"- {m}: {', '.join(sorted(fns))}" for m, fns in sorted(by_mod.items()))


def versions_for(graph: dict) -> dict[str, str]:
    """{distribution: version} for the library ops a graph uses (+ numpy),
    recorded with published versions so results can be reproduced."""
    pkgs = {"numpy"}
    for n in graph.get("nodes") or []:
        parts = split_name(str(n.get("op") or ""))
        if parts:
            pkgs.add(MODULES[parts[0]])
    out = {}
    for p in sorted(pkgs):
        try:
            out[p] = importlib.metadata.version(p)
        except importlib.metadata.PackageNotFoundError:
            pass
    return out
