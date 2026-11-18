"""Published pipelines as nodes (D-045)."""
import numpy as np
import pytest
from PIL import Image

from src.backend.api.pipeline_routes import run_pipeline_for_session
from src.backend.core.pipeline import subgraph
from src.backend.core.session.manager import get_manager
from tests.test_publishing import CODE_V1, PORTS

def _inner(cid):
    return {"nodes": [{"id": "g", "op": "to_grayscale"},
                      {"id": "inv", "custom_node_id": cid},
                      {"id": "o", "op": "otsu_threshold", "params": {"invert": False}}],
            "edges": [{"from": ["input", "image"], "to": ["g", "image"]},
                      {"from": ["g", "gray"], "to": ["inv", "image"]},
                      {"from": ["inv", "image"], "to": ["o", "image"]}]}


def _publish(c, title, graph):
    pid = c.post("/api/pipelines", json={"title": title, "graph": graph}).get_json()["pipeline_id"]
    r = c.post(f"/api/pipelines/{pid}/publish", json={}).get_json()
    assert r.get("published_id"), r
    return pid, r["published_id"], r["version"]


def _outer(pub, v, **params):
    return {"nodes": [{"id": "s", "pipeline": {"published_id": pub, "version": v}, "params": params},
                      {"id": "n", "op": "to_number", "params": {"reduce": "count_nonzero"}}],
            "edges": [{"from": ["input", "image"], "to": ["s", "image"]},
                      {"from": ["s", "mask"], "to": ["n", "value"]}]}


@pytest.fixture
def published(make_user):
    ca, ua, _ = make_user()
    cid = ca.post("/api/custom-nodes", json={"name": "invert", "code": CODE_V1, "ports": PORTS,
                                             "doc": "invert"}).get_json()["custom_node_id"]
    pid, pub, v = _publish(ca, "gray invert otsu", _inner(cid))
    return ca, ua, pid, pub, v


def test_interface_and_validation(published, make_user):
    ca, _, _, pub, v = published
    cb, ub, _ = make_user()
    itf = cb.get(f"/api/published/{pub}/node?version={v}").get_json()
    assert [p["name"] for p in itf["inputs"]] == ["image"]
    assert [(o["name"], o["type"]) for o in itf["outputs"]] == [("mask", "MASK")]
    assert {"o.nbins", "o.invert"} <= {p["name"] for p in itf["params"]}
    assert next(p for p in itf["params"] if p["name"] == "o.invert")["default"] is False

    ok = cb.post("/api/pipelines/validate", json={"graph": _outer(pub, v)}).get_json()
    assert ok["valid"], ok
    bad = cb.post("/api/pipelines/validate", json={"graph": _outer(pub, v, **{"o.nbins": "lots"})}).get_json()
    assert not bad["valid"] and bad["issues"][0]["node_id"] == "s" and bad["issues"][0]["message"].startswith("[o]")
    unk = cb.post("/api/pipelines/validate", json={"graph": _outer(pub, v, **{"zz.q": 1})}).get_json()
    assert "Unknown param 'zz.q'" in unk["issues"][0]["message"]
    nover = _outer(pub, v)
    del nover["nodes"][0]["pipeline"]["version"]
    assert "pinned" in cb.post("/api/pipelines/validate", json={"graph": nover}).get_json()["issues"][0]["message"]
    port = _outer(pub, v)
    port["edges"][1]["from"] = ["s", "labels"]
    assert "Outputs: mask" in cb.post("/api/pipelines/validate", json={"graph": port}).get_json()["issues"][0]["message"]


def test_expansion_namespaces_custom_nodes(published):
    _, ua, _, pub, v = published
    plain, customs, outer = subgraph.expand(_outer(pub, v), ua, {})
    assert {n["id"] for n in plain["nodes"]} == {"s/g", "s/inv", "s/o", "n"}
    inv = next(n for n in plain["nodes"] if n["id"] == "s/inv")
    assert inv["custom_node_id"] == next(iter(customs)) and inv["custom_node_id"].startswith(f"{pub}@{v}:")
    assert {"from": ["input", "image"], "to": ["s/g", "image"]} in plain["edges"]
    assert {"from": ["s/o", "mask"], "to": ["n", "value"]} in plain["edges"]
    assert outer == {"s": {"mask": ["s/o", "mask"]}}


@pytest.mark.slow
def test_run_nested_override_unpublish(published, make_user, upload):
    ca, ua, pid, pub, v = published
    cb, ub, _ = make_user()
    iid = upload(cb)
    sid = get_manager().create_session(user_id=ub, image_ids=[iid], title="sub").session_id

    r = run_pipeline_for_session(sid, _outer(pub, v), "uses sub", ub)
    assert not r["error"], r
    n = r["values"][0]["n"]["number"]
    assert n > 0
    alone = {"nodes": [_outer(pub, v)["nodes"][0]], "edges": [_outer(pub, v)["edges"][0]]}
    r = run_pipeline_for_session(sid, alone, "sub alone", ub)
    assert not r["error"], r
    out = next(o for o in get_manager().get_session(sid).outputs if "s~o" in o.description)
    assert np.asarray(Image.open(out.path)).any(), "unconsumed inner outputs are final outputs"

    r = run_pipeline_for_session(sid, _outer(pub, v, **{"o.invert": True}), "inverted", ub)
    assert not r["error"], r
    assert r["values"][0]["n"]["number"] != n, "override reaches the inner node"

    # A published pipeline that itself contains a pipeline node.
    _, pub2, v2 = _publish(ca, "wrapper", {"nodes": [{"id": "w", "pipeline": {"published_id": pub, "version": v}}],
                                           "edges": [{"from": ["input", "image"], "to": ["w", "image"]}]})
    plain, _, _ = subgraph.expand(_outer(pub2, v2), ub, {})
    assert {"s/w/o", "s/w/inv"} <= {n["id"] for n in plain["nodes"]}
    r = run_pipeline_for_session(sid, _outer(pub2, v2), "nested", ub)
    assert not r["error"] and r["values"][0]["n"]["number"] == n

    assert ca.post(f"/api/published/{pub}/unpublish").status_code in (200, 204)
    r = run_pipeline_for_session(sid, _outer(pub, v), "gone", ub)
    assert "unpublished" in r["issues"][0]["message"]
