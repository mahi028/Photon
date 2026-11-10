"""Saved models and painted labels (D-044)."""
import base64
import io

import numpy as np
import pytest
from PIL import Image

from src.backend.api.pipeline_routes import run_pipeline_for_session
from src.backend.core.pipeline import runner, saved
from src.backend.core.pipeline.ops import ml
from src.backend.core.session.manager import get_manager


def _png(a):
    buf = io.BytesIO()
    Image.fromarray(a.astype(np.uint8)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _scribbles(h=96, w=128):
    """disk.png: disk centred at (48, 64) r=30. 1 = disk, 2 = background."""
    y = np.zeros((h, w), np.uint8)
    y[44:52, 58:70] = 1
    y[2:8, 2:20] = 2
    y[88:94, 100:124] = 2
    return y


TRAIN = {"nodes": [{"id": "rf", "op": "lib:sklearn.ensemble.RandomForestClassifier",
                    "params": {"n_estimators": 10, "random_state": 0}},
                   {"id": "y", "op": "load_labels"},
                   {"id": "fit", "op": "ml_fit", "params": {"ignore_label": 0}},
                   {"id": "save", "op": "ml_save_model", "params": {"name": "disk rf"}}],
         "edges": [{"from": ["rf", "model"], "to": ["fit", "model"]},
                   {"from": ["input", "image"], "to": ["fit", "X"]},
                   {"from": ["y", "labels"], "to": ["fit", "y"]},
                   {"from": ["fit", "model"], "to": ["save", "model"]}]}


def apply_graph(model_id):
    return {"nodes": [{"id": "m", "op": "ml_load_model", "params": {"model_id": model_id}},
                      {"id": "p", "op": "ml_predict"}],
            "edges": [{"from": ["m", "model"], "to": ["p", "model"]},
                      {"from": ["input", "image"], "to": ["p", "X"]}]}


def _session(uid, iid):
    return get_manager().create_session(user_id=uid, image_ids=[iid], title="ml").session_id


def test_annotation_api(make_user, upload):
    c, _, _ = make_user()
    other, _, _ = make_user()
    iid = upload(c)
    r = c.post("/api/annotations", json={"image_id": iid, "png": _png(_scribbles()), "name": "scribbles",
                                         "classes": [{"value": 1, "name": "disk", "color": "#ff0000"},
                                                     {"value": 2, "name": "bg", "color": "#0000ff"}]})
    assert r.status_code == 201, r.get_json()
    a = r.get_json()
    assert a["width"] == 128 and a["counts"] == {"1": 96, "2": 252}
    assert c.post("/api/annotations", json={"image_id": iid, "png": _png(np.zeros((10, 10)))}).status_code == 400
    assert c.post("/api/annotations", json={"image_id": iid, "png": _png(np.full((96, 128), 99))}).status_code == 400
    assert other.post("/api/annotations", json={"image_id": iid, "png": _png(_scribbles())}).status_code == 404
    assert other.get(f"/api/annotations/{a['annotation_id']}/image").status_code == 404
    png = c.get(f"/api/annotations/{a['annotation_id']}/image")
    assert (np.asarray(Image.open(io.BytesIO(png.data))) == _scribbles()).all()
    upd = c.post("/api/annotations", json={"image_id": iid, "png": _png(_scribbles() * 0),
                                           "annotation_id": a["annotation_id"]})
    assert upd.status_code == 200 and upd.get_json()["counts"] == {}
    assert [x["annotation_id"] for x in c.get(f"/api/annotations?image_id={iid}").get_json()["annotations"]] \
        == [a["annotation_id"]]
    assert other.delete(f"/api/annotations/{a['annotation_id']}").status_code == 404
    assert c.delete(f"/api/annotations/{a['annotation_id']}").status_code == 204


def test_skops_load_refuses_other_types():
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    X = np.random.default_rng(0).random((20, 2))
    pipe = Pipeline([("s", StandardScaler())]).fit(X)
    with pytest.raises(ValueError, match="not a Photon model node class"):
        ml.loads_model(ml.dumps_model(pipe))
    m = ml.loads_model(ml.dumps_model(StandardScaler().fit(X)))
    assert np.allclose(m.mean_, X.mean(0))


def test_graph_checks():
    assert runner.validate(TRAIN) == []
    issues = runner.validate({"nodes": [{"id": "m", "op": "ml_load_model"}], "edges": []})
    assert "Required param 'model_id'" in " ".join(i.message for i in issues)


@pytest.mark.slow
def test_train_save_load_publish(make_user, upload):
    c1, u1, _ = make_user()
    c2, u2, _ = make_user()
    iid = upload(c1)
    sid = _session(u1, iid)

    r = run_pipeline_for_session(sid, TRAIN, "train", u1)
    assert "no painted labels" in (r["error"] or ""), r
    assert c1.post("/api/annotations", json={"image_id": iid, "png": _png(_scribbles())}).status_code == 201

    r = run_pipeline_for_session(sid, TRAIN, "train", u1)
    assert not r["error"], r["error"]
    [m] = r["saved_models"]
    assert m["name"] == "disk rf" and m["estimator"] == "RandomForestClassifier"
    assert [x["model_id"] for x in c1.get("/api/models").get_json()["models"]] == [m["model_id"]]
    assert c2.get("/api/models").get_json()["models"] == []

    r = run_pipeline_for_session(sid, apply_graph(m["model_id"]), "apply", u1)
    assert not r["error"], r["error"]
    out = get_manager().get_session(sid).outputs[-1]
    labels = np.asarray(Image.open(out.path))
    assert labels[48, 64] == 1 and labels[5, 120] == 2 and labels[90, 5] == 2

    iid2 = upload(c2)
    sid2 = _session(u2, iid2)
    r = run_pipeline_for_session(sid2, apply_graph(m["model_id"]), "steal", u2)
    assert "not found or is not yours" in r["error"]

    pid = c1.post("/api/pipelines", json={"title": "disk rf", "graph": apply_graph(m["model_id"])}).get_json()["pipeline_id"]
    pub = c1.post(f"/api/pipelines/{pid}/publish", json={}).get_json()
    assert pub.get("published_id"), pub
    run = c2.post(f"/api/sessions/{sid2}/run-pipeline", json={"published_id": pub["published_id"]})
    assert run.status_code == 202
    import time
    for _ in range(600):
        time.sleep(0.25)
        get_manager().invalidate(sid2)
        if get_manager().get_session(sid2).status == "idle":
            break
    assert get_manager().get_session(sid2).outputs, "published pipeline loads the author's model"

    assert c1.delete(f"/api/models/{m['model_id']}").status_code == 204
    assert saved.model_path(m["model_id"]).is_file(), "published model kept after delete"
    assert c1.get("/api/models").get_json()["models"] == []

    bad = {"nodes": [{"id": "y", "op": "load_labels", "params": {"annotation_id": "a" * 32}}], "edges": []}
    pid2 = c1.post("/api/pipelines", json={"title": "x", "graph": bad}).get_json()["pipeline_id"]
    assert "private" in c1.post(f"/api/pipelines/{pid2}/publish", json={}).get_json()["error"]
