"""Library nodes (D-041): allow-listed library functions as pipeline ops."""
import json
import tempfile

import numpy as np
import pytest
from PIL import Image

from src.backend.config import config
from src.backend.core.pipeline import library, registry, runner
from src.backend.core.pipeline.types import PortType, can_connect

MULTIOTSU = {
    "nodes": [{"id": "g", "op": "to_grayscale"},
              {"id": "t", "op": "lib:skimage.filters.threshold_multiotsu", "params": {"classes": 3}},
              {"id": "d", "op": "digitize"}],
    "edges": [{"from": ["input", "image"], "to": ["g", "image"]},
              {"from": ["g", "gray"], "to": ["t", "image"]},
              {"from": ["g", "gray"], "to": ["d", "image"]},
              {"from": ["t", "thresh"], "to": ["d", "bins"]}]}


def _ports(spec):
    return ([(p.name, p.optional) for p in spec.inputs], [(p.name, p.type) for p in spec.outputs])


def test_specs_from_signatures_and_docstrings():
    ins, outs = _ports(library.get("lib:skimage.filters.threshold_multiotsu"))
    assert ("image", True) in ins and outs == [("thresh", PortType.ARRAY)]
    ins, outs = _ports(library.get("lib:skimage.segmentation.watershed"))
    assert ("image", False) in ins and ("markers", True) in ins and ("mask", True) in ins
    ins, outs = _ports(library.get("lib:skimage.filters.gabor"))
    assert [o for o, _ in outs] == ["real", "imag"]
    ins, outs = _ports(library.get("lib:cv2.threshold"))
    assert ins == [("src", False)] and outs == [("retval", PortType.NUMBER), ("dst", PortType.ARRAY)]
    cv = library.get("lib:cv2.GaussianBlur")
    assert {p.name for p in cv.params if p.required} == {"ksize", "sigmaX"}
    assert "dst" not in {p.name for p in cv.params}, "output buffers are hidden"
    nd = library.get("lib:scipy.ndimage.gaussian_filter")
    assert "output" not in {p.name for p in nd.params} and [p.name for p in nd.inputs] == ["input"]
    lab = library.get("lib:skimage.measure.label")
    assert [o for o, _ in _ports(lab)[1]] == ["labels", "num"]
    assert library.get("lib:skimage.filters.rank.median") is not None, "nested module"


@pytest.mark.parametrize("name", [
    "lib:skimage.io.imread", "lib:skimage.data.camera", "lib:cv2.imwrite", "lib:cv2.selectROI",
    "lib:os.system", "lib:subprocess.run", "lib:builtins.eval", "lib:numpy.load",
    "lib:skimage.filters.__class__", "lib:skimage.filters.threshold_otsu.__globals__",
    "lib:skimage.filters._gaussian", "lib:skimage.util.apply_parallel", "lib:skimage.measure.ransac",
    "lib:cv2.setNumThreads", "lib:skimage..filters", "skimage.filters.threshold_otsu", "lib:",
])
def test_denied_or_unknown_names(name):
    assert library.get(name) is None
    assert registry.get_op(name) is None or not name.startswith("lib:")


def test_catalogue_size_and_search():
    specs = library.all_specs()
    assert len(specs) > 400
    assert not [n for n in specs if any(w in n.lower() for w in ("imread", "imwrite", "imsave", "load"))]
    hits = [s.name for s in library.search("multiotsu")]
    assert hits and hits[0] == "lib:skimage.filters.threshold_multiotsu"
    assert library.search("") == []


def test_wildcard_types():
    assert can_connect(PortType.ARRAY, PortType.IMAGE_GRAY)[0]
    assert can_connect(PortType.MASK, PortType.ARRAY)[0]
    assert can_connect(PortType.ANY, PortType.MASK)[0]
    assert can_connect(PortType.NUMBER, PortType.ARRAY)[0]
    assert not can_connect(PortType.NUMBER, PortType.IMAGE_GRAY)[0]
    assert not can_connect(PortType.IMAGE_RGB, PortType.IMAGE_GRAY)[0], "explicit to_grayscale still required"


def test_validation_messages_name_the_real_ports():
    assert runner.validate(MULTIOTSU) == []
    bad = json.loads(json.dumps(MULTIOTSU))
    bad["edges"][3]["from"] = ["t", "thresholds"]
    bad["nodes"][1]["params"]["klasses"] = 2
    msgs = " ".join(i.message for i in runner.validate(bad))
    assert "Outputs: thresh" in msgs and "Params: classes, nbins" in msgs
    cv = {"nodes": [{"id": "c", "op": "lib:cv2.GaussianBlur", "params": {"sigmaX": 0}}],
          "edges": [{"from": ["input", "image"], "to": ["c", "src"]}]}
    assert "Required param 'ksize'" in " ".join(i.message for i in runner.validate(cv))
    unknown = {"nodes": [{"id": "x", "op": "lib:skimage.io.imread"}], "edges": []}
    assert "not available" in runner.validate(unknown)[0].message


def test_api_search_and_lookup(make_user):
    c, _, _ = make_user()
    core = c.get("/api/ops").get_json()["ops"]
    assert not [o for o in core if o["name"].startswith("lib:")] and any(o["category"] == "glue" for o in core)
    r = c.get("/api/ops/library?q=watershed").get_json()
    assert r["total"] > 400 and "lib:skimage.segmentation.watershed" in [o["name"] for o in r["ops"]]
    r = c.get("/api/ops/library?names=lib:cv2.threshold,lib:os.system").get_json()
    assert [o["name"] for o in r["ops"]] == ["lib:cv2.threshold"]
    assert r["ops"][0]["library"]["package"].startswith("opencv")


def _run(graph, disk_png):
    from src.backend.core.sandbox.backend import run_pipeline
    out = tempfile.mkdtemp(dir=config.TMP_EXEC_DIR)
    return run_pipeline(graph, disk_png, out, out + "/nodes")


@pytest.fixture(scope="module")
def ramp_png(tmp_path_factory):
    img = (np.add.outer(np.arange(96), np.arange(96)) % 256).astype(np.uint8)
    p = tmp_path_factory.mktemp("lib") / "ramp.png"
    Image.fromarray(np.stack([img] * 3, -1)).save(p)
    return str(p)


@pytest.mark.slow
def test_run_multiotsu_cv2_and_values(ramp_png):
    r = _run(MULTIOTSU, ramp_png)
    assert not r["error"], r["error"]
    assert [f["node_id"] for f in r["finals"]] == ["d"]
    assert len(r["values"]["t"]["thresh"]) == 2, "threshold array reported as a value"
    labels = np.asarray(Image.open(r["finals"][0]["path"]))
    assert len(np.unique(labels)) == 3

    cv = {"nodes": [{"id": "g", "op": "to_grayscale"},
                    {"id": "c", "op": "lib:cv2.threshold",
                     "params": {"thresh": 0, "maxval": 255, "type": "THRESH_BINARY|THRESH_OTSU"}},
                    {"id": "b", "op": "lib:cv2.GaussianBlur", "params": {"ksize": [5, 5], "sigmaX": 0}}],
          "edges": [{"from": ["input", "image"], "to": ["g", "image"]},
                    {"from": ["g", "gray"], "to": ["c", "src"]},
                    {"from": ["c", "dst"], "to": ["b", "src"]}]}
    r = _run(cv, ramp_png)
    assert not r["error"], r["error"]
    assert 0 < r["values"]["c"]["retval"] < 255 and r["finals"][0]["node_id"] == "b"


@pytest.mark.slow
def test_run_tables_tuples_and_optional_ports(ramp_png):
    g = {"nodes": [{"id": "g", "op": "to_grayscale"}, {"id": "o", "op": "otsu_threshold"},
                   {"id": "l", "op": "lib:skimage.measure.label", "params": {"return_num": True}},
                   {"id": "p", "op": "lib:skimage.measure.regionprops_table",
                    "params": {"properties": ["label", "area"]}},
                   {"id": "w", "op": "lib:skimage.segmentation.watershed"},   # markers/mask unconnected
                   {"id": "n", "op": "to_number", "params": {"reduce": "max"}}],
         "edges": [{"from": ["input", "image"], "to": ["g", "image"]},
                   {"from": ["g", "gray"], "to": ["o", "image"]},
                   {"from": ["o", "mask"], "to": ["l", "label_image"]},
                   {"from": ["l", "labels"], "to": ["p", "label_image"]},
                   {"from": ["g", "gray"], "to": ["w", "image"]},
                   {"from": ["l", "labels"], "to": ["w", "markers"]},
                   {"from": ["l", "num"], "to": ["n", "value"]}]}
    assert runner.validate(g) == []
    r = _run(g, ramp_png)
    assert not r["error"], r["error"]
    assert r["values"]["p"]["out_dict"]["area"], "dict table summarised"
    assert r["values"]["n"]["number"] >= 1 and r["values"]["l"]["num"] >= 1
    assert "w" in {f["node_id"] for f in r["finals"]}


@pytest.mark.slow
def test_library_function_errors_are_reported(ramp_png):
    g = {"nodes": [{"id": "s", "op": "lib:scipy.ndimage.gaussian_filter", "params": {"sigma": "big"}}],
         "edges": [{"from": ["input", "image"], "to": ["s", "input"]}]}
    r = _run(g, ramp_png)
    assert r["failed_node"] == "s" and r["error"]


def test_publish_records_library_versions(make_user):
    c, _, _ = make_user()
    pid = c.post("/api/pipelines", json={"title": "multiotsu", "graph": MULTIOTSU}).get_json()["pipeline_id"]
    pub = c.post(f"/api/pipelines/{pid}/publish", json={}).get_json()["published_id"]
    env = c.get(f"/api/published/{pub}").get_json()["environment"]
    assert set(env) == {"numpy", "scikit-image"}
