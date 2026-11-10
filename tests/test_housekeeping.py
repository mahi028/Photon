"""Disk housekeeping, quota, closed sign-up default (D-048)."""
import io
import json
import os
import time
from contextlib import closing
from pathlib import Path

import pytest

from src.backend.config import config
from src.backend.core import housekeeping as hk
from src.backend.core.session import store


def test_signup_defaults_to_closed(monkeypatch):
    from src.backend.core.auth.signup_policy import SignupPolicy
    monkeypatch.delenv("PHOTON_SIGNUP", raising=False)
    assert SignupPolicy.from_env().mode == "closed"
    monkeypatch.setenv("PHOTON_SIGNUP", "open")
    assert SignupPolicy.from_env().mode == "open"


def test_quota_blocks_uploads_runs_and_reports_usage(make_user, upload, disk_png, monkeypatch):
    c, uid, _ = make_user()
    iid = upload(c)
    u = c.get("/auth/me/usage?fresh=1").get_json()
    assert u["images"] > 0 and u["quota"] == 0 and u["total"] >= u["images"]
    size = os.path.getsize(disk_png)
    monkeypatch.setenv("PHOTON_USER_QUOTA_GB", str((u["total"] + size // 2) / (1 << 30)))
    hk.forget_usage()
    with open(disk_png, "rb") as f:
        r = c.post("/api/upload", data={"file": (f, "more.png")}, content_type="multipart/form-data")
    assert r.status_code == 507 and "quota" in r.get_json()["error"].lower()
    monkeypatch.setenv("PHOTON_USER_QUOTA_GB", str(u["total"] * 0.5 / (1 << 30)))   # already over
    hk.forget_usage()
    sid = c.post("/api/sessions", json={"image_ids": [iid]}).get_json()["session_id"]
    g = {"nodes": [{"id": "g", "op": "to_grayscale"}], "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}
    r = c.post(f"/api/sessions/{sid}/run-pipeline", json={"graph": g})
    assert r.status_code == 507
    r = c.post("/api/batches", json={"graph": g, "image_ids": [iid]})
    assert r.status_code == 507
    assert c.get("/auth/me/usage").get_json()["quota"] > 0


def test_sweeps(make_user):
    tmp = Path(config.TMP_EXEC_DIR)
    old, new = tmp / "pipe_old_test", tmp / "pipe_new_test"
    for d in (old, new):
        d.mkdir(exist_ok=True)
        (d / "run.py").write_text("x")
    past = time.time() - 7 * 3600
    os.utime(old, (past, past))
    gdir = config.VOLUMES_DIR / "model_grants"
    gdir.mkdir(exist_ok=True)
    (gdir / ("a" * 48)).write_text(json.dumps({"model_id": "x", "expires": time.time() - 1}))
    (gdir / ("b" * 48)).write_text(json.dumps({"model_id": "x", "expires": time.time() + 3600}))
    n = hk.sweep_tmp()
    assert not old.exists() and new.exists() and n["tmp"] >= 1
    assert not (gdir / ("a" * 48)).exists() and (gdir / ("b" * 48)).exists()

    _, uid, _ = make_user()
    from src.backend.core.pipeline import batch
    bid = "c" * 32
    store.create_batch({"batch_id": bid, "user_id": uid, "title": "old", "graph": {"nodes": [], "edges": []},
                        "status": "done", "created_at": "2001-01-01T00:00:00"}, [])
    store.update_batch(bid, finished_at="2001-01-02T00:00:00", status="done")
    batch.batch_dir(bid).mkdir(parents=True, exist_ok=True)
    assert hk.sweep_batches(days=0) == 0 and store.get_batch(bid)
    assert hk.sweep_batches(days=30) >= 1
    assert store.get_batch(bid) is None and not batch.batch_dir(bid).exists()


@pytest.mark.slow
def test_run_deletes_its_scratch(disk_png, tmp_path):
    from src.backend.core.pipeline.executor import run_pipeline_subprocess
    before = {p.name for p in Path(config.TMP_EXEC_DIR).glob("pipe_*")}
    g = {"nodes": [{"id": "g", "op": "to_grayscale"}], "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}
    r = run_pipeline_subprocess(g, disk_png, str(tmp_path / "o"), str(tmp_path / "n"))
    assert not r["error"] and Path(r["finals"][0]["path"]).is_file()
    assert {p.name for p in Path(config.TMP_EXEC_DIR).glob("pipe_*")} == before
