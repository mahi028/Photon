"""Machine-learning ops (D-042): fit and apply scikit-learn models in a pipeline.

A model node (`lib:sklearn.cluster.KMeans`, `lib:sklearn.ensemble.
RandomForestClassifier`, ...) makes an unfitted MODEL. These ops fit it and
apply it, on one of two sample layouts:

  pixels  every pixel is a sample. X is an image (H, W) or a feature stack
          (H, W, C), e.g. from lib:skimage.feature.multiscale_basic_features;
          y, if given, is a label image (H, W). Results come back as images:
          labels (H, W), probabilities / components (H, W, K).
  rows    every row is a sample: X is a table (N, C), or a dict of columns
          such as lib:skimage.measure.regionprops_table returns; y has N
          entries. Results are (N,) or (N, K) arrays; map_labels paints
          per-object results back onto a label image.

Models normally live only for the run. To keep one (D-044), ml_save_model
writes it as a skops file (a format that refuses to load unknown types, unlike
pickle); after the run the backend registers it as a saved model, and
ml_load_model brings it back in any later graph, published ones included.
load_labels reads a label image painted in the UI, for y.

The parent stages those files into the run's scratch dir before the child
starts and sets RUNTIME["assets"] (D-044); the child never sees the store.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import numpy as np

from ..registry import Param, Port, register
from ..types import PortType

_A, _M = PortType.ARRAY, PortType.MODEL

# Set by the pipeline child (executor._CHILD): staged files ({"model:<id>":
# path, "labels:<id>": path}), where ml_save_model writes, and what it wrote.
RUNTIME: dict = {"assets": {}, "save_dir": None, "saved": []}
MAX_MODEL_BYTES = 256 << 20

# Internal types skops doesn't trust by default but our estimators contain.
# Anything else in a file is refused (D-044).
_SKOPS_TRUSTED = {
    "sklearn.tree._tree.Tree",
    "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
    "sklearn.neighbors._kd_tree.KDTree",
    "sklearn.neighbors._ball_tree.BallTree",
}
_SKOPS_TRUSTED_RE = re.compile(r"^sklearn\.metrics\._dist_metrics\.\w+(32|64)$")


def _allowed_class(model) -> bool:
    from ..library import ESTIMATORS
    import importlib
    for name in ESTIMATORS:
        mod, cls = name.rsplit(".", 1)
        try:
            if type(model) is getattr(importlib.import_module(mod), cls):
                return True
        except Exception:
            continue
    return False


def dumps_model(model) -> bytes:
    import skops.io as sio
    return sio.dumps(model)


def loads_model(data: bytes):
    """skops load that accepts only the estimator classes Photon exposes."""
    import skops.io as sio
    untrusted = sio.get_untrusted_types(data=data)
    bad = [t for t in untrusted if t not in _SKOPS_TRUSTED and not _SKOPS_TRUSTED_RE.match(t)]
    if bad:
        raise ValueError(f"saved model contains types Photon won't load: {bad}")
    model = sio.loads(data, trusted=untrusted)
    if not _allowed_class(model):
        raise ValueError(f"saved model is a {type(model).__name__}, not a Photon model node class")
    return model
_SAMPLES = Param("samples", "choice", "pixels", choices=["pixels", "rows"],
                 doc="pixels: each pixel of an (H,W) or (H,W,C) image is a sample. "
                     "rows: each row of an (N,C) table, or of a dict of columns, is a sample.")


def _table(X, columns=None) -> np.ndarray:
    """dict of columns (regionprops_table) or array -> 2-D float array."""
    if isinstance(X, dict):
        keys = list(columns) if columns else [k for k, v in X.items()
                                              if np.asarray(v).dtype.kind in "biuf" and np.asarray(v).ndim == 1]
        if not keys:
            raise ValueError("table has no numeric columns")
        missing = [k for k in keys if k not in X]
        if missing:
            raise KeyError(f"no column(s) {missing}; columns are {sorted(X)}")
        return np.column_stack([np.asarray(X[k], dtype=np.float64) for k in keys])
    a = np.asarray(X)
    if a.ndim == 1:
        return a.reshape(-1, 1).astype(np.float64)
    if a.ndim != 2:
        raise ValueError(f"rows mode needs an (N, C) table, got shape {a.shape}")
    return a.astype(np.float64)


def _pixels(X) -> tuple[np.ndarray, tuple]:
    a = np.asarray(X)
    if a.ndim == 2:
        return a.reshape(-1, 1).astype(np.float64), a.shape
    if a.ndim == 3:
        return a.reshape(-1, a.shape[2]).astype(np.float64), a.shape[:2]
    raise ValueError(f"pixels mode needs an (H, W) or (H, W, C) image, got shape {a.shape}")


def _samples(X, samples):
    if samples == "pixels":
        return _pixels(X)
    return _table(X), None


def _training_set(X2, y, shape, samples, ignore_label, max_samples, random_state):
    """Rows to fit on: drop ignored labels, then subsample for speed."""
    yv = None
    if y is not None:
        ya = np.asarray(y)
        yv = ya.reshape(-1) if samples == "pixels" else ya.ravel()
        if yv.shape[0] != X2.shape[0]:
            raise ValueError(f"y has {yv.shape[0]} samples but X has {X2.shape[0]}"
                             + (f" (image {shape})" if shape else ""))
    idx = np.arange(X2.shape[0])
    if yv is not None and ignore_label is not None:
        idx = idx[yv != ignore_label]
        if idx.size == 0:
            raise ValueError(f"every sample has the ignored label {ignore_label}")
    if max_samples and idx.size > max_samples:
        idx = np.random.default_rng(random_state).choice(idx, int(max_samples), replace=False)
        idx.sort()
    return X2[idx], (yv[idx] if yv is not None else None)


def _clone(model):
    from sklearn.base import clone
    return clone(model)


def _apply(model, X2, shape, method):
    if not hasattr(model, method):
        have = [m for m in ("predict", "predict_proba", "transform", "decision_function",
                            "score_samples") if hasattr(model, m)]
        raise ValueError(f"{type(model).__name__} has no {method}(); it has {have}")
    out = np.asarray(getattr(model, method)(X2))
    if shape is not None:
        out = out.reshape(shape) if out.ndim == 1 else out.reshape(*shape, out.shape[1])
    return out


_FIT_PARAMS = [
    _SAMPLES,
    Param("ignore_label", "int", None, nullable=True,
          doc="Samples whose y equals this are not used for fitting (e.g. 0 = unlabeled). Empty: use all."),
    Param("max_samples", "int", 200000, min=100,
          doc="Fit on at most this many samples (random subset), to keep runs fast."),
    Param("random_state", "int", 0, doc="Seed for the subset."),
]


def _check_model(model):
    if not hasattr(model, "get_params"):
        raise TypeError(f"'model' must come from a model node (lib:sklearn...), got {type(model).__name__}")


@register(
    name="ml_fit", category="ml",
    inputs=[Port("model", _M), Port("X", _A), Port("y", _A, optional=True)],
    outputs=[Port("model", _M)],
    params=list(_FIT_PARAMS),
    doc="Fit a model on X (and labels y for supervised models). Returns a fitted copy.",
)
def ml_fit(model, X, y=None, samples="pixels", ignore_label=None, max_samples=200000, random_state=0):
    _check_model(model)
    X2, shape = _samples(X, samples)
    Xf, yf = _training_set(X2, y, shape, samples, ignore_label, max_samples, random_state)
    m = _clone(model)
    m.fit(Xf) if yf is None else m.fit(Xf, yf)
    return {"model": m}


@register(
    name="ml_predict", category="ml",
    inputs=[Port("model", _M), Port("X", _A)],
    outputs=[Port("result", _A)],
    params=[_SAMPLES,
            Param("method", "choice", "predict",
                  choices=["predict", "predict_proba", "transform", "decision_function", "score_samples"])],
    doc="Apply a fitted model to X: labels (predict), class probabilities, or transformed "
        "features. Pixel mode returns images.",
)
def ml_predict(model, X, samples="pixels", method="predict"):
    _check_model(model)
    X2, shape = _samples(X, samples)
    return {"result": _apply(model, X2, shape, method)}


@register(
    name="ml_fit_predict", category="ml",
    inputs=[Port("model", _M), Port("X", _A), Port("y", _A, optional=True)],
    outputs=[Port("result", _A), Port("model", _M)],
    params=list(_FIT_PARAMS) + [
        Param("method", "choice", "predict",
              choices=["predict", "predict_proba", "transform", "decision_function", "score_samples"],
              doc="Applied to ALL samples after fitting. Clusterers without predict "
                  "(DBSCAN, Agglomerative...) use fit_predict on all samples instead."),
    ],
    doc="Fit on X (and y), then apply to every sample: e.g. k-means clusters of pixels, or "
        "a random-forest pixel classifier trained from a coarse label image.",
)
def ml_fit_predict(model, X, y=None, samples="pixels", ignore_label=None, max_samples=200000,
                   random_state=0, method="predict"):
    _check_model(model)
    X2, shape = _samples(X, samples)
    m = _clone(model)
    if not hasattr(m, method) and method == "predict" and hasattr(m, "fit_predict"):
        if max_samples and X2.shape[0] > max_samples:
            raise ValueError(f"{type(m).__name__} can't predict new samples, so it must see all "
                             f"{X2.shape[0]}; raise max_samples or downscale the image first")
        out = m.fit_predict(X2) if y is None else m.fit_predict(X2, np.asarray(y).ravel())
        out = np.asarray(out)
        return {"result": out.reshape(shape) if shape is not None else out, "model": m}
    Xf, yf = _training_set(X2, y, shape, samples, ignore_label, max_samples, random_state)
    m.fit(Xf) if yf is None else m.fit(Xf, yf)
    return {"result": _apply(m, X2, shape, method), "model": m}


@register(
    name="table_to_array", category="ml",
    inputs=[Port("table", PortType.ANY)],
    outputs=[Port("array", _A)],
    params=[Param("columns", "json", None, nullable=True,
                  doc='Columns to keep, in order, e.g. ["area", "eccentricity"]. Empty: every numeric column.')],
    doc="A dict of columns (regionprops_table) or a table as an (N, C) float array.",
)
def table_to_array(table, columns=None):
    return {"array": _table(table, columns)}


@register(
    name="map_labels", category="ml",
    inputs=[Port("label_image", _A), Port("values", _A), Port("labels", _A, optional=True)],
    outputs=[Port("image", _A)],
    params=[Param("background", "float", 0.0, doc="Value for pixels whose label has no value (and label 0).")],
    doc="Paint one value per object onto a label image: values[i] goes to label labels[i] "
        "(default 1..N, the order regionprops_table uses). E.g. object clusters as an image.",
)
def map_labels(label_image, values, labels=None, background=0.0):
    lab = np.asarray(label_image)
    vals = np.asarray(values).ravel()
    ids = np.arange(1, vals.size + 1) if labels is None else np.asarray(labels).ravel()
    if ids.size != vals.size:
        raise ValueError(f"{vals.size} values but {ids.size} labels")
    lut = dict(zip(ids.tolist(), vals.tolist()))
    flat = lab.ravel()
    uniq, inv = np.unique(flat, return_inverse=True)
    mapped = np.array([lut.get(u, background) if u != 0 else background for u in uniq.tolist()])
    out = mapped[inv].reshape(lab.shape)
    return {"image": out}


def _is_fitted(model) -> bool:
    from sklearn.utils.validation import check_is_fitted
    try:
        check_is_fitted(model)
        return True
    except Exception:
        return False


@register(
    name="ml_save_model", category="ml",
    inputs=[Port("model", _M)],
    outputs=[Port("model", _M)],
    params=[Param("name", "str", None, required=True,
                  doc="Name it is saved under. Each run saves a new copy (see Saved models).")],
    doc="Keep a fitted model after the run. It appears under Saved models; load it in any "
        "pipeline with ml_load_model to apply it to other images.",
)
def ml_save_model(model, name=None):
    _check_model(model)
    if not _is_fitted(model):
        raise ValueError("model is not fitted: connect the model output of ml_fit or ml_fit_predict")
    if not _allowed_class(model):
        raise ValueError(f"{type(model).__name__} can't be saved")
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("name is required")
    try:
        data = dumps_model(model)
    except Exception as e:
        raise ValueError(f"{type(model).__name__} can't be saved in the safe format: {e}") from None
    if len(data) > MAX_MODEL_BYTES:
        raise ValueError(f"model is {len(data) >> 20} MB; the limit is {MAX_MODEL_BYTES >> 20} MB "
                         "(fewer trees or max_depth makes it smaller)")
    if RUNTIME["save_dir"] is None:
        raise RuntimeError("saving models is only possible inside a pipeline run")
    d = Path(RUNTIME["save_dir"])
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex}.skops"
    path.write_bytes(data)
    RUNTIME["saved"].append({"name": name, "path": str(path), "estimator": type(model).__name__,
                             "summary": repr(model)[:300]})
    return {"model": model}


@register(
    name="ml_load_model", category="ml",
    inputs=[],
    outputs=[Port("model", _M)],
    params=[Param("model_id", "str", None, required=True,
                  doc="A saved model (see Saved models, or list_models over MCP).")],
    doc="A fitted model saved by an earlier run (ml_save_model). Connect it to ml_predict.",
)
def ml_load_model(model_id=None):
    path = RUNTIME["assets"].get(f"model:{model_id}")
    if not path:
        raise ValueError(f"saved model {model_id!r} was not found or is not yours")
    return {"model": loads_model(Path(path).read_bytes())}


@register(
    name="load_labels", category="ml",
    inputs=[],
    outputs=[Port("labels", _A)],
    params=[Param("annotation_id", "str", "",
                  doc="A label image painted in the session view. Empty: the most recent one "
                      "painted on the image being processed.")],
    doc="Labels painted over the input image in the UI (0 = unlabeled, 1..N = classes), as an "
        "(H, W) int array: y for ml_fit / ml_fit_predict with ignore_label=0.",
)
def load_labels(annotation_id=""):
    path = RUNTIME["assets"].get(f"labels:{annotation_id or ''}")
    if not path:
        raise ValueError("no painted labels for this image" if not annotation_id
                         else f"annotation {annotation_id!r} was not found or is not yours")
    from PIL import Image
    return {"labels": np.asarray(Image.open(path)).astype(np.int32)}
