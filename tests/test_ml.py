"""Machine-learning nodes (D-042): scikit-learn model constructors + fit/predict ops."""
import tempfile

import numpy as np
import pytest

from src.backend.config import config
from src.backend.core.pipeline import library, registry, runner
from src.backend.core.pipeline.types import PortType, can_connect


def op(name):
    return registry.get_op(name).fn


def _blobs():
    rng = np.random.default_rng(0)
    img = np.zeros((40, 40, 3), np.uint8)
    img[:, :20] = (200, 40, 40)
    img[:, 20:] = (40, 40, 200)
    img[10:25, 10:25] = (40, 200, 40)
    return np.clip(img + rng.normal(0, 8, img.shape), 0, 255).astype(np.uint8)


def test_estimator_specs():
    km = library.get("lib:sklearn.cluster.KMeans")
    assert km.inputs == [] and [(o.name, o.type) for o in km.outputs] == [("model", PortType.MODEL)]
    names = {p.name for p in km.params}
    assert {"n_clusters", "random_state"} <= names and "verbose" not in names
    assert next(p for p in km.params if p.name == "n_clusters").kind == "int"
    rf = library.get("lib:sklearn.ensemble.RandomForestClassifier")
    assert "n_jobs" not in {p.name for p in rf.params}, "parallelism is never exposed"
    assert "predict_proba" in rf.library["methods"]
    assert library.get("lib:sklearn.cluster.DBSCAN").library["methods"] == ["fit", "fit_predict"]


@pytest.mark.parametrize("name", ["lib:sklearn.datasets.fetch_openml", "lib:sklearn.utils.Bunch",
                                  "lib:sklearn.cluster.k_means", "lib:sklearn.ensemble.VotingClassifier",
                                  "lib:sklearn.model_selection.GridSearchCV", "lib:joblib.load"])
def test_only_listed_estimators(name):
    assert library.get(name) is None


def test_model_type_rules():
    assert can_connect(PortType.MODEL, PortType.MODEL)[0]
    assert can_connect(PortType.ANY, PortType.MODEL)[0]
    assert not can_connect(PortType.MODEL, PortType.ARRAY)[0]
    assert not can_connect(PortType.ARRAY, PortType.MODEL)[0]


def test_kmeans_pixels_and_fitted_copy():
    model = op("lib:sklearn.cluster.KMeans")(n_clusters=3, n_init=3, random_state=0)["model"]
    out = op("ml_fit_predict")(model=model, X=_blobs())
    assert out["result"].shape == (40, 40) and len(np.unique(out["result"])) == 3
    assert not hasattr(model, "cluster_centers_"), "input model left unfitted (cloned)"
    again = op("ml_predict")(model=out["model"], X=_blobs())["result"]
    assert (again == out["result"]).all()


def test_supervised_pixels_ignore_label_and_proba():
    img = _blobs()
    y = np.zeros((40, 40), np.int32)
    y[:5, :5] = 1                       # a few red scribbles
    y[:5, 35:] = 2                      # a few blue scribbles; everything else unlabeled (0)
    rf = op("lib:sklearn.ensemble.RandomForestClassifier")(n_estimators=10, random_state=0)["model"]
    labels = op("ml_fit_predict")(model=rf, X=img, y=y, ignore_label=0)["result"]
    assert set(np.unique(labels)) == {1, 2}
    assert (labels[:, :18] == 1).mean() > 0.9 and (labels[:, 27:] == 2).mean() > 0.9
    proba = op("ml_fit_predict")(model=rf, X=img, y=y, ignore_label=0, method="predict_proba")["result"]
    assert proba.shape == (40, 40, 2)
    with pytest.raises(ValueError, match="ignored label"):
        op("ml_fit")(model=rf, X=img, y=np.zeros((40, 40)), ignore_label=0)
    with pytest.raises(ValueError, match="samples but X has"):
        op("ml_fit")(model=rf, X=img, y=np.zeros((10, 10)))


def test_rows_table_and_map_labels():
    table = {"label": np.array([1, 2, 3, 4]), "area": np.array([10.0, 11.0, 300.0, 310.0])}
    X = op("table_to_array")(table=table, columns=["area"])["array"]
    assert X.shape == (4, 1)
    km = op("lib:sklearn.cluster.KMeans")(n_clusters=2, n_init=3, random_state=0)["model"]
    c = op("ml_fit_predict")(model=km, X=X, samples="rows")["result"]
    assert c[0] == c[1] != c[2] == c[3]
    lab = np.array([[0, 1, 2], [3, 4, 0]])
    img = op("map_labels")(label_image=lab, values=c + 10)["image"]
    assert img[0, 0] == 0 and img[0, 1] == c[0] + 10 and img[1, 1] == c[3] + 10
    with pytest.raises(KeyError, match="columns are"):
        op("table_to_array")(table=table, columns=["nope"])


def test_clusterer_without_predict_uses_fit_predict():
    db = op("lib:sklearn.cluster.DBSCAN")(eps=30)["model"]
    out = op("ml_fit_predict")(model=db, X=_blobs(), max_samples=10000)["result"]
    assert out.shape == (40, 40) and len(np.unique(out)) >= 3
    with pytest.raises(ValueError, match="has no predict"):
        op("ml_predict")(model=db, X=_blobs())
    with pytest.raises(TypeError, match="model node"):
        op("ml_fit")(model=np.zeros(3), X=_blobs())


@pytest.mark.slow
def test_kmeans_pipeline_in_sandbox(disk_png):
    from src.backend.core.sandbox.backend import run_pipeline
    g = {"nodes": [{"id": "k", "op": "lib:sklearn.cluster.KMeans", "params": {"n_clusters": 2, "n_init": 2}},
                   {"id": "f", "op": "ml_fit_predict", "params": {"max_samples": 2000}}],
         "edges": [{"from": ["k", "model"], "to": ["f", "model"]},
                   {"from": ["input", "image"], "to": ["f", "X"]}]}
    assert runner.validate(g) == []
    out = tempfile.mkdtemp(dir=config.TMP_EXEC_DIR)
    r = run_pipeline(g, disk_png, out, out + "/nodes", timeout=900)
    assert not r["error"], r["error"]
    assert [f["node_id"] for f in r["finals"]] == ["f"]
    assert "fitted" in r["values"]["f"]["model"] and "not fitted" in r["values"]["k"]["model"]
