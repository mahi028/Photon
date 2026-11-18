"""Published pipelines (D-035): versions, frozen custom-node snapshots,
runs by other users, fork, unpublish."""
import json
import time

import numpy as np
import pytest
from PIL import Image

from src.backend.core.session.manager import get_manager

CODE_V1 = ("def node(inputs, params):\n    import numpy as np\n    img = inputs['image'].astype(np.float32)\n"
           "    return {'image': (255 - img).clip(0, 255).astype(np.uint8)}\n")
CODE_V2 = CODE_V1.replace("255 - img", "(255 - img) * 0.5")
PORTS = {"inputs": [{"name": "image", "type": "IMAGE_GRAY"}], "outputs": [{"name": "image", "type": "IMAGE_GRAY"}]}


def graph_for(cid):
    return {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}}, {"id": "inv", "custom_node_id": cid, "params": {}}],
            "edges": [{"from": ["input", "image"], "to": ["g", "image"]}, {"from": ["g", "gray"], "to": ["inv", "image"]}]}


def run_and_wait(c, sid, body):
    r = c.post(f"/api/sessions/{sid}/run-pipeline", json=body)
    if r.status_code != 202:
        return r.status_code, None
    m = get_manager()
    for _ in range(240):
        time.sleep(0.25)
        m.invalidate(sid)
        sess = m.get_session(sid)
        if sess.status == "idle":
            return 202, sess.outputs
    raise TimeoutError


@pytest.mark.slow
def test_publish_run_fork_unpublish(make_user, upload):
    c1, _, u1name = make_user()
    c2, _, _ = make_user()
    cid = c1.post("/api/custom-nodes", json={"name": "invert", "code": CODE_V1, "ports": PORTS, "doc": "invert"}).get_json()["custom_node_id"]
    graph = graph_for(cid)
    pid = c1.post("/api/pipelines", json={"title": "Invert", "description": "gray then invert", "graph": graph}).get_json()["pipeline_id"]

    r = c1.post(f"/api/pipelines/{pid}/publish", json={"changelog": "first"})
    assert r.status_code == 201 and r.get_json()["version"] == 1
    pub = r.get_json()["published_id"]
    r = c1.post(f"/api/pipelines/{pid}/publish", json={})
    assert r.status_code == 200 and not r.get_json()["created"], "unchanged republish makes no version"
    assert c2.post(f"/api/pipelines/{pid}/publish", json={}).status_code == 404
    bad = c1.post("/api/pipelines", json={"title": "bad", "graph": {"nodes": [{"id": "x", "op": "nope"}], "edges": []}}).get_json()["pipeline_id"]
    r = c1.post(f"/api/pipelines/{bad}/publish", json={})
    assert r.status_code == 400 and r.get_json().get("issues")

    # node code change alone -> v2
    c1.post("/api/custom-nodes", json={"name": "invert", "code": CODE_V2, "ports": PORTS, "doc": "invert"})
    assert c1.post(f"/api/pipelines/{pid}/publish", json={"changelog": "halve"}).get_json()["version"] == 2

    listed = next(p for p in c2.get("/api/published").get_json() if p["published_id"] == pub)
    assert not listed["is_mine"] and listed["uses_custom_nodes"] and listed["node_count"] == 2
    assert any(p["published_id"] == pub for p in c2.get("/api/published?q=invert").get_json())
    assert not any(p["published_id"] == pub for p in c2.get("/api/published?q=zzzz").get_json())
    d1 = c2.get(f"/api/published/{pub}?version=1").get_json()
    assert d1["custom_nodes"][cid]["code"] == CODE_V1 and len(d1["versions"]) == 2
    assert c2.get(f"/api/published/{pub}?version=9").status_code == 404

    # author deletes the node: runs still use the frozen snapshot
    c1.delete(f"/api/custom-nodes/{cid}")
    sid = c2.post("/api/sessions", json={"title": "run", "image_ids": [upload(c2)]}).get_json()["session_id"]
    code, outs = run_and_wait(c2, sid, {"published_id": pub, "version": 1})
    prov = json.loads(outs[-1].code)
    a1 = np.asarray(Image.open(outs[-1].path))
    assert code == 202 and prov["published_id"] == pub and prov["version"] == 1
    code, outs = run_and_wait(c2, sid, {"published_id": pub})
    a2 = np.asarray(Image.open(outs[-1].path))
    assert json.loads(outs[-1].code)["version"] == 2 and a2.max() <= 128 < a1.max()
    assert run_and_wait(c2, sid, {"graph": graph})[0] == 400, "raw custom_node_id of another user must not resolve"

    # fork: import nodes, reuse identical, rename on clash
    f1 = c2.post(f"/api/published/{pub}/fork", json={"version": 1}).get_json()
    assert f1["imported_nodes"][0]["name"] == "invert" and not f1["imported_nodes"][0]["reused"]
    fp = c2.get(f"/api/pipelines/{f1['pipeline_id']}").get_json()
    assert c2.post("/api/pipelines/validate", json={"graph": fp["graph"]}).get_json()["valid"]
    assert "Forked from" in fp["description"]
    assert c2.post(f"/api/published/{pub}/fork", json={"version": 1}).get_json()["imported_nodes"][0]["reused"]
    n = c2.post(f"/api/published/{pub}/fork", json={"version": 2}).get_json()["imported_nodes"][0]
    assert not n["reused"] and n["name"] == f"invert (from {u1name})"

    # unpublish hides it from others, owner still sees it
    assert c2.post(f"/api/published/{pub}/unpublish").status_code == 404
    assert c1.post(f"/api/published/{pub}/unpublish").status_code == 200
    assert not any(p["published_id"] == pub for p in c2.get("/api/published").get_json())
    assert c2.get(f"/api/published/{pub}").status_code == 404
    assert run_and_wait(c2, sid, {"published_id": pub})[0] == 404
    assert any(p["published_id"] == pub and p["unpublished_at"] for p in c1.get("/api/published").get_json())

    # republish restores as v3 of the same publication; survives source deletion
    cid2 = c1.post("/api/custom-nodes", json={"name": "invert", "code": CODE_V2, "ports": PORTS, "doc": "invert"}).get_json()["custom_node_id"]
    c1.patch(f"/api/pipelines/{pid}", json={"graph": graph_for(cid2)})
    r = c1.post(f"/api/pipelines/{pid}/publish", json={}).get_json()
    assert r["version"] == 3 and r["published_id"] == pub
    c1.delete(f"/api/pipelines/{pid}")
    assert c2.get(f"/api/published/{pub}").status_code == 200
    html = c2.get(f"/published/{pub}?version=1").get_data(as_text=True)
    assert f'window.PUBLISHED_ID = "{pub}"' in html and "window.PUBLISHED_VERSION = 1" in html


@pytest.mark.slow
def test_card_thumbnail_from_authors_latest_output(make_user, upload):
    author, _, _ = make_user()
    other, _, _ = make_user()
    graph = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}, "pos": {"x": 1, "y": 2}}],
             "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}
    pid = author.post("/api/pipelines", json={"title": "Thumb", "graph": graph}).get_json()["pipeline_id"]

    def card(c, pub):
        return next(p for p in c.get("/api/published").get_json() if p["published_id"] == pub)

    # no run yet: no image, card falls back to the op chain
    r = author.post(f"/api/pipelines/{pid}/publish", json={}).get_json()
    pub = r["published_id"]
    assert r["thumbnail"] is False and card(other, pub)["thumbnail_url"] is None
    assert other.get(f"/api/published/{pub}/thumbnail").status_code == 404

    # author runs it (editor layout differs: pos is ignored when matching)
    sid = author.post("/api/sessions", json={"image_ids": [upload(author)]}).get_json()["session_id"]
    moved = json.loads(json.dumps(graph)); moved["nodes"][0]["pos"] = {"x": 50, "y": 60}
    assert run_and_wait(author, sid, {"graph": moved, "title": "t"})[0] == 202

    # unchanged re-publish refreshes the image; everyone can fetch it
    r = author.post(f"/api/pipelines/{pid}/publish", json={}).get_json()
    assert not r["created"] and r["thumbnail"] is True
    url = card(other, pub)["thumbnail_url"]
    resp = other.get(url)
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg"
    assert max(Image.open(__import__("io").BytesIO(resp.data)).size) <= 480

    # opting out removes it; unpublished hides it from others
    assert author.post(f"/api/pipelines/{pid}/publish", json={"thumbnail": False}).get_json()["thumbnail"] is False
    assert other.get(f"/api/published/{pub}/thumbnail").status_code == 404
    assert author.post(f"/api/pipelines/{pid}/publish", json={}).get_json()["thumbnail"] is True
    author.post(f"/api/published/{pub}/unpublish")
    assert other.get(f"/api/published/{pub}/thumbnail").status_code == 404
    assert author.get(f"/api/published/{pub}/thumbnail").status_code == 200

    # a new version with a different graph drops the stale image
    graph2 = {"nodes": [{**graph["nodes"][0], "id": "g2"}],
              "edges": [{"from": ["input", "image"], "to": ["g2", "image"]}]}
    assert author.patch(f"/api/pipelines/{pid}", json={"graph": graph2}).status_code == 200
    r = author.post(f"/api/pipelines/{pid}/publish", json={}).get_json()
    assert r["created"] and r["thumbnail"] is False
