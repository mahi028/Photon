"""Uploaded segmentation models (D-047): spec, safe loading, grants, sidecar run."""
import io
import json
import os
import pickle
import time

import numpy as np
import pytest

from src.backend.core.models import user_models
from src.backend.core.pipeline import runner

SPEC = {"name": "tiny tissue", "arch": "Segformer", "encoder": "mit_b0", "in_channels": 3,
        "classes": 1, "activation": "sigmoid", "input_size": [64, 96], "normalize": "imagenet",
        "threshold": 0.5, "postprocess": {"closing_radius": 1, "min_component_frac": 0.001, "fill_holes": True}}


def test_spec_validation():
    s = user_models.validate_spec(SPEC)
    assert s["color"] == "rgb" and s["postprocess"]["fill_holes"] is True
    for bad, msg in [({"arch": "MyNet"}, "arch must be"), ({"encoder": "../x"}, "encoder must be"),
                     ({"input_size": [100, 96]}, "multiples of 32"), ({"classes": 1, "activation": "softmax"}, "softmax"),
                     ({"normalize": "custom"}, "mean and std"), ({"name": ""}, "name is required"),
                     ({"color": "gray"}, "gray goes with")]:
        with pytest.raises(user_models.SpecError, match=msg):
            user_models.validate_spec({**SPEC, **bad})


def test_graph_node_validates():
    g = {"nodes": [{"id": "m", "op": "segment_with_model", "params": {"model_id": "a" * 32}}],
         "edges": [{"from": ["input", "image"], "to": ["m", "image"]}]}
    assert runner.validate(g) == []
    g["nodes"][0]["params"] = {}
    assert "Required param 'model_id'" in runner.validate(g)[0].message


class _Evil:
    def __reduce__(self):
        return (os.system, (f"touch {_Evil.marker}",))


def _wait_status(c, mid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/models/{mid}").get_json()
        if r["status"] != "checking":
            return r
        time.sleep(1)
    raise TimeoutError(r)


@pytest.fixture(scope="module")
def tiny_weights(tmp_path_factory):
    import torch
    spec = user_models.validate_spec(SPEC)
    net = user_models.build(spec)
    p = tmp_path_factory.mktemp("w") / "tiny.pth"
    torch.save({"state_dict": {f"module.{k}": v for k, v in net.state_dict().items()}}, p)   # wrapped + DataParallel
    return p


@pytest.mark.slow
def test_upload_check_and_refusals(make_user, tiny_weights, tmp_path):
    c, _, _ = make_user()
    other, _, _ = make_user()
    data = tiny_weights.read_bytes()
    r = c.post("/api/models", content_type="multipart/form-data",
               data={"weights": (io.BytesIO(data), "tiny.pth"), "spec": json.dumps(SPEC)})
    assert r.status_code == 202, r.get_json()
    ok = _wait_status(c, r.get_json()["model_id"])
    assert ok["status"] == "ready", ok
    assert ok["info"]["output_shape"] == [1, 1, 64, 96] and ok["info"]["parameters"] > 1_000_000
    assert other.get(f"/api/models/{ok['model_id']}").status_code == 404

    wrong = c.post("/api/models", content_type="multipart/form-data",
                   data={"weights": (io.BytesIO(data), "tiny.pth"), "spec": json.dumps({**SPEC, "classes": 3,
                                                                                          "activation": "softmax"})})
    bad = _wait_status(c, wrong.get_json()["model_id"])
    assert bad["status"] == "failed" and "size mismatch" in bad["error"].lower() or "match" in bad["error"]

    _Evil.marker = str(tmp_path / "pwned")
    evil = pickle.dumps({"state_dict": _Evil()})
    r = c.post("/api/models", content_type="multipart/form-data",
               data={"weights": (io.BytesIO(evil), "evil.pth"), "spec": json.dumps(SPEC)})
    res = _wait_status(c, r.get_json()["model_id"])
    assert res["status"] == "failed" and not os.path.exists(_Evil.marker), "weights_only refused the pickle"

    assert c.post("/api/models", content_type="multipart/form-data",
                  data={"weights": (io.BytesIO(b"x"), "w.exe"), "spec": json.dumps(SPEC)}).status_code == 400
    assert c.post("/api/models", content_type="multipart/form-data",
                  data={"weights": (io.BytesIO(data), "w.pth"), "spec": "{}"}).status_code == 400


@pytest.mark.slow
def test_grants_and_sidecar_run(make_user, tiny_weights, tmp_path):
    from src.backend.core.models.sidecar_server import _handle
    from src.backend.core.session import store
    c, uid, _ = make_user()
    _, other_uid, _ = make_user()
    with open(tiny_weights, "rb") as f:
        row = user_models.create(uid, SPEC, "tiny.pth", f, run_check=False)
    mid = row["model_id"]
    graph = {"nodes": [{"id": "m", "op": "segment_with_model", "params": {"model_id": mid}}], "edges": []}
    with pytest.raises(user_models.SpecError, match="isn't ready"):
        user_models.issue_grants(graph, uid)                    # still 'checking'
    store.update_model(mid, status="ready")
    with pytest.raises(user_models.SpecError):
        user_models.issue_grants(graph, other_uid)              # not theirs
    grants = user_models.issue_grants(graph, uid)
    assert user_models.check_grant(grants[mid], mid) and not user_models.check_grant(grants[mid], "b" * 32)

    img = tmp_path / "img.npy"
    np.save(img, np.random.default_rng(0).integers(0, 255, (50, 70, 3), dtype=np.uint8))
    req = {"cmd": "run_op", "op": "segment_with_model", "inputs": {"image": str(img)},
           "params": {"model_id": mid}, "out_dir": str(tmp_path / "out")}
    assert "No permission" in _handle(dict(req))["error"]
    assert "No permission" in _handle({**req, "params": {"model_id": mid, "_grant": "0" * 48}})["error"]
    res = _handle({**req, "params": {"model_id": mid, "_grant": grants[mid]}})
    assert res["ok"], res
    mask = np.load(res["outputs"]["mask"])
    prob = np.load(res["outputs"]["probability"])
    assert mask.shape == prob.shape == (50, 70) and mask.dtype == bool and 0 <= prob.min() <= prob.max() <= 1
    user_models.revoke_grants(grants)
    assert not user_models.check_grant(grants[mid], mid)


def test_pre_and_post_processing():
    """A hand-made 1x1 conv: foreground = bright red channel. Checks resize,
    normalisation, thresholding, small-component removal and hole filling."""
    import torch
    from src.backend.core.pipeline.ops.models import segment_with_model
    spec = user_models.validate_spec({**SPEC, "normalize": "zero_one", "input_size": [64, 64],
                                      "postprocess": {"min_component_frac": 0.01, "fill_holes": True}})
    net = torch.nn.Conv2d(3, 1, 1)
    with torch.no_grad():
        net.weight.zero_(); net.weight[0, 0] = 20.0; net.bias.fill_(-10.0)   # logit > 0 where R > 0.5
    handle = user_models.Handle(net.eval(), spec, "cpu")
    img = np.zeros((128, 192, 3), np.uint8)
    img[20:100, 30:150, 0] = 255                 # big red block ...
    img[50:60, 80:90, 0] = 0                     # ... with a hole
    img[5:7, 5:7, 0] = 255                       # and a speck
    out = segment_with_model(img, handle, model_id="x")
    m = out["mask"]
    assert m.shape == (128, 192)
    assert m[55, 85], "hole filled"
    assert not m[6, 6], "speck removed"
    assert m[60, 100] and not m[110, 10]
    raw = segment_with_model(img, handle, model_id="x", postprocess=False)["mask"]
    assert not raw[55, 85]


def test_models_api_lists_both_kinds(make_user, tiny_weights):
    from src.backend.core.session import store
    c, uid, _ = make_user()
    with open(tiny_weights, "rb") as f:
        seg = user_models.create(uid, SPEC, "tiny.pth", f, run_check=False)
    store.add_model({"model_id": "e" * 32, "user_id": uid, "kind": "sklearn", "name": "rf", "file": "models/x.skops",
                     "size_bytes": 10, "status": "ready", "spec": {"estimator": "RandomForestClassifier"},
                     "created_at": "2026-01-01T00:00:00"})
    kinds = {m["kind"] for m in c.get("/api/models").get_json()["models"]}
    assert kinds == {"sklearn", "segmentation"}
    only = c.get("/api/models?kind=sklearn").get_json()["models"]
    assert [m["model_id"] for m in only] == ["e" * 32] and only[0]["estimator"] == "RandomForestClassifier"
    assert c.get("/api/models?kind=segmentation&status=ready").get_json()["models"] == []   # still checking
    assert c.get("/api/models?kind=nope").status_code == 400
    assert c.get(f"/api/models/{seg['model_id']}").get_json()["kind"] == "segmentation"
    assert c.delete(f"/api/models/{seg['model_id']}").status_code == 204
    assert c.get(f"/api/models/{seg['model_id']}").status_code == 404


def test_migration_from_old_tables():
    """D-049: rows of trained_models / seg_models move into `models` with ids and files kept."""
    from contextlib import closing
    from src.backend.core.session import store
    with closing(store._get_conn()) as conn, conn:
        conn.execute("""CREATE TABLE trained_models (model_id TEXT PRIMARY KEY, user_id TEXT, name TEXT, estimator TEXT,
                        size_bytes INTEGER, versions_json TEXT, session_id TEXT, pipeline_title TEXT, summary TEXT,
                        created_at TEXT, published INTEGER DEFAULT 0, deleted_at TEXT)""")
        conn.execute("""CREATE TABLE seg_models (model_id TEXT PRIMARY KEY, user_id TEXT, name TEXT, spec_json TEXT,
                        weights_file TEXT, size_bytes INTEGER, sha256 TEXT, status TEXT, error TEXT, info_json TEXT,
                        created_at TEXT, published INTEGER DEFAULT 0, deleted_at TEXT)""")
        conn.execute("INSERT INTO trained_models VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("1" * 32, "u1", "old rf", "RandomForestClassifier", 123, '{"numpy": "2"}', "s1", "train", "RF()",
                      "2026-01-01", 1, None))
        conn.execute("INSERT INTO seg_models VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("2" * 32, "u1", "old seg", json.dumps(SPEC), "2" * 32 + ".pth", 456, "ab", "ready", None,
                      '{"parameters": 5}', "2026-01-02", 0, None))
    store.init_db()
    a, b = store.get_model("1" * 32), store.get_model("2" * 32)
    assert a["kind"] == "sklearn" and a["file"] == f"trained_models/{'1' * 32}.skops" and a["published"]
    assert a["spec"]["estimator"] == "RandomForestClassifier" and a["spec"]["versions"] == {"numpy": "2"}
    assert b["kind"] == "segmentation" and b["file"] == f"model_weights/{'2' * 32}.pth" and b["info"]["parameters"] == 5
    with closing(store._get_conn()) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "trained_models" not in names and "seg_models" not in names
