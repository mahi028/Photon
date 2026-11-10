"""Batch runs (D-046)."""
import csv
import io
import time
import zipfile
from contextlib import closing

import pytest

from src.backend.core.pipeline import batch
from src.backend.core.session import store

GRAPH = {"nodes": [{"id": "g", "op": "to_grayscale"}, {"id": "o", "op": "otsu_threshold"},
                   {"id": "l", "op": "lib:skimage.measure.label", "params": {"return_num": True}},
                   {"id": "p", "op": "lib:skimage.measure.regionprops_table",
                    "params": {"properties": ["label", "area"]}},
                   {"id": "n", "op": "to_number", "params": {"reduce": "max"}},
                   {"id": "b", "op": "gaussian_blur", "params": {"sigma": 2.0}}],
         "edges": [{"from": ["input", "image"], "to": ["g", "image"]},
                   {"from": ["g", "gray"], "to": ["o", "image"]},
                   {"from": ["o", "mask"], "to": ["l", "label_image"]},
                   {"from": ["l", "labels"], "to": ["p", "label_image"]},
                   {"from": ["l", "num"], "to": ["n", "value"]},
                   {"from": ["g", "gray"], "to": ["b", "image"]}]}


def _wait(c, bid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = c.get(f"/api/batches/{bid}").get_json()
        if b["status"] not in ("queued", "running"):
            return b
        time.sleep(0.5)
    raise TimeoutError(b)


def _upload(c, disk_png):
    png = open(disk_png, "rb").read()
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("folder/c.png", png)
        z.writestr("folder/notes.txt", b"hello")
        z.writestr("__MACOSX/._c.png", b"junk")
    r = c.post("/api/batches/upload", content_type="multipart/form-data",
               data={"files": [(io.BytesIO(png), "a.png"), (io.BytesIO(png), "b.png"),
                               (io.BytesIO(zbuf.getvalue()), "more.zip")]})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_upload_and_create_checks(make_user, disk_png):
    c, _, _ = make_user()
    other, _, _ = make_user()
    up = _upload(c, disk_png)
    assert [i["filename"] for i in up["images"]] == ["a.png", "b.png", "c.png"]
    assert [s["filename"] for s in up["skipped"]] == ["notes.txt"]
    ids = [i["image_id"] for i in up["images"]]
    assert other.post("/api/batches", json={"graph": GRAPH, "image_ids": ids}).status_code == 404
    assert c.post("/api/batches", json={"graph": GRAPH}).status_code == 400
    bad = {"nodes": [{"id": "x", "op": "nope"}], "edges": []}
    r = c.post("/api/batches", json={"graph": bad, "image_ids": ids})
    assert r.status_code == 400 and r.get_json()["issues"]
    assert c.post("/api/batches", json={"graph": GRAPH, "pipeline_id": "x", "image_ids": ids}).status_code == 400


@pytest.mark.slow
def test_batch_end_to_end(make_user, disk_png):
    c, _, _ = make_user()
    other, _, _ = make_user()
    ids = [i["image_id"] for i in _upload(c, disk_png)["images"]]
    r = c.post("/api/batches", json={"graph": GRAPH, "image_ids": ids, "title": "objects"})
    assert r.status_code == 201, r.get_json()
    bid = r.get_json()["batch_id"]
    assert other.get(f"/api/batches/{bid}").status_code == 404
    b = _wait(c, bid)
    assert b["status"] == "done" and b["done"] == 3 and b["failed"] == 0, b
    assert "n.number" in b["columns"] and b["tables"][0]["key"] == "p.out_dict"
    assert "p.out_dict" not in b["columns"], "tables get their own CSV, not a JSON blob column"
    assert all(it["outputs"] for it in b["items"])
    assert all(it["input_preview"].startswith("/previews/uploads/") for it in b["items"])
    assert all(it["preview"] and it["preview"]["node_id"] for it in b["items"]), "last image made is kept"
    assert c.get(it_prev := f"/api/batches/{bid}/files/{b['items'][0]['preview']['path']}").status_code == 200
    prev = b["items"][0]["outputs"][0]["preview"]
    assert c.get(f"/api/batches/{bid}/files/{prev}").status_code == 200
    assert c.get(f"/api/batches/{bid}/files/../../sessions.db").status_code == 404

    rows = list(csv.DictReader(io.StringIO(c.get(f"/api/batches/{bid}/values.csv").data.decode())))
    assert [r["filename"] for r in rows] == ["a.png", "b.png", "c.png"]
    assert all(float(r["n.number"]) >= 1 for r in rows)
    t = list(csv.DictReader(io.StringIO(c.get(f"/api/batches/{bid}/table.csv?key=p.out_dict").data.decode())))
    assert {r["filename"] for r in t} == {"a.png", "b.png", "c.png"} and "area" in t[0]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/batches/{bid}/download.zip").data))
    names = z.namelist()
    assert "values.csv" in names and any(n.startswith("tables/") for n in names)
    assert sum(n.startswith("outputs/") for n in names) >= 3

    # A runner that died mid-batch: the next process takes it over.
    with closing(store._get_conn()) as conn, conn:
        conn.execute("UPDATE batches SET status='running', runner_pid=2147483000, finished_at=NULL WHERE batch_id=?", (bid,))
        conn.execute("UPDATE batch_items SET status='running' WHERE batch_id=? AND idx=2", (bid,))
    assert batch.recover() == 1
    b = _wait(c, bid)
    assert b["status"] == "done" and b["done"] == 3

    assert c.delete(f"/api/batches/{bid}").status_code == 204
    assert not batch.batch_dir(bid).exists()


@pytest.mark.slow
def test_failures_cancel_retry(make_user, disk_png):
    c, _, _ = make_user()
    ids = [i["image_id"] for i in _upload(c, disk_png)["images"]]
    g = {"nodes": [{"id": "s", "op": "lib:scipy.ndimage.gaussian_filter", "params": {"sigma": "big"}}],
         "edges": [{"from": ["input", "image"], "to": ["s", "input"]}]}
    bid = c.post("/api/batches", json={"graph": g, "image_ids": ids}).get_json()["batch_id"]
    b = _wait(c, bid)
    assert b["status"] == "done" and b["failed"] == 3 and b["items"][0]["error"]
    assert c.post(f"/api/batches/{bid}/retry").status_code == 200
    b = _wait(c, bid)
    assert b["failed"] == 3
    bid2 = c.post("/api/batches", json={"graph": GRAPH, "image_ids": ids * 1}).get_json()["batch_id"]
    c.post(f"/api/batches/{bid2}/cancel")
    b2 = _wait(c, bid2)
    assert b2["status"] in ("cancelled", "done") and b2["done"] + b2["cancelled"] == 3


def test_batch_page_component_attribute(make_user):
    """x-data must survive HTML parsing: tojson emits double quotes (blank-page bug)."""
    import re
    c, _, _ = make_user()
    html = c.get("/batches/056e9176aa304722ac86ef0fea5a9f5f").data.decode()
    assert re.search(r"x-data='batchPage\(\"056e9176aa304722ac86ef0fea5a9f5f\"\)'", html), \
        re.findall(r"x-data=.{0,60}", html)
