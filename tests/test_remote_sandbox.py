"""PHOTON_SANDBOX=remote end to end, against a sandbox worker started on
localhost (no gVisor here: this checks the protocol and the app switch)."""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
import requests

from src.backend.config import config
from src.backend.core.sandbox import backend
from src.backend.core.sandbox.remote import RemoteSandboxError, safe_extract

REPO = Path(__file__).resolve().parent.parent
TOKEN = "test-token-" + os.urandom(8).hex()
GRAY = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}}],
        "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "out/../../x", "other/x", "out\\..\\x"])
def test_safe_extract_rejects(bad, tmp_path):
    with pytest.raises(RemoteSandboxError):
        safe_extract(_zip({"result.json": "{}", bad: "x"}), {"out": tmp_path})


def test_safe_extract_ok(tmp_path):
    r = safe_extract(_zip({"result.json": '{"a": 1}', "out/sub/f.png": "x"}), {"out": tmp_path})
    assert r == {"a": 1} and (tmp_path / "sub/f.png").read_text() == "x"


@pytest.fixture(scope="module")
def worker():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PHOTON_SANDBOX_TOKEN": TOKEN,
           "PHOTON_VOLUMES_DIR": tempfile.mkdtemp(prefix="photon-worker-test-")}
    proc = subprocess.Popen([sys.executable, "-m", "src.sandbox_worker.server", "--port", str(port)],
                            cwd=REPO, env=env, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180          # cold imports on a network FS can be slow
    while True:
        try:
            requests.get(url + "/v1/health", timeout=2)
            break
        except requests.RequestException:
            if proc.poll() is not None:
                pytest.fail(f"sandbox worker exited with {proc.returncode}")
            if time.time() > deadline:
                proc.kill()
                pytest.fail("sandbox worker did not start within 180 s")
            time.sleep(0.5)
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture
def remote(worker, monkeypatch):
    monkeypatch.setenv("PHOTON_SANDBOX", "remote")
    monkeypatch.setenv("PHOTON_SANDBOX_URL", worker)
    monkeypatch.setenv("PHOTON_SANDBOX_TOKEN", TOKEN)
    return worker



@pytest.mark.slow
def test_worker_auth(worker, disk_png):
    assert requests.get(worker + "/v1/health", timeout=10).json()["ok"]
    for h in ({}, {"Authorization": "Bearer nope"}):
        r = requests.post(worker + "/v1/run-code", headers=h,
                          data={"job": "{}"}, files={"input": open(disk_png, "rb")}, timeout=30)
        assert r.status_code == 401


@pytest.mark.slow
def test_remote_pipeline_and_code(remote, disk_png):
    out = Path(tempfile.mkdtemp(dir=config.TMP_EXEC_DIR))
    r = backend.run_pipeline(GRAY, disk_png, str(out), str(out / "nodes"))
    assert not r["error"], r["error"]
    assert r["finals"] and Path(r["finals"][0]["path"]).is_file()
    assert Path(r["finals"][0]["path"]).parent == out
    assert all(Path(p["path"]).is_file() for p in r["previews"].values())

    code = ("from PIL import Image\nimport os\ndef main(input_path, output_dir):\n"
            "    p = os.path.join(output_dir, 'o.png'); Image.open(input_path).convert('L').save(p); return p\n")
    res = backend.get_executor().execute(code, disk_png, str(out / "rc"), 60)
    assert res.file_exists and Path(res.output_path).parent == out / "rc"

    res = backend.get_executor().execute(code.replace("return p", "open('/etc/passwd').read(); return p"),
                                         disk_png, str(out / "rc2"), 60)
    assert "Blocked by sandbox" in (res.traceback or "") + res.stderr
    res = backend.get_executor().execute("import subprocess\ndef main(i, o):\n    pass\n", disk_png, str(out / "rc3"), 60)
    assert "Blocked by sandbox policy" in res.stderr


@pytest.mark.slow
def test_unreachable_worker_is_a_clean_error(monkeypatch, disk_png):
    monkeypatch.setenv("PHOTON_SANDBOX", "remote")
    monkeypatch.setenv("PHOTON_SANDBOX_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PHOTON_SANDBOX_TOKEN", "x")
    out = tempfile.mkdtemp(dir=config.TMP_EXEC_DIR)
    assert "Remote sandbox error" in backend.run_pipeline(GRAY, disk_png, out, out + "/n")["error"]
    assert "Remote sandbox error" in backend.get_executor().execute("def main(i,o): pass", disk_png, out, 5).stderr


@pytest.mark.slow
def test_app_run_route_uses_remote(remote, make_user, upload):
    from src.backend.core.session.manager import get_manager
    c, _, _ = make_user()
    sid = c.post("/api/sessions", json={"image_ids": [upload(c)]}).get_json()["session_id"]
    assert c.post(f"/api/sessions/{sid}/run-pipeline", json={"graph": GRAY}).status_code == 202
    m = get_manager()
    for _ in range(120):
        time.sleep(0.25)
        m.invalidate(sid)
        if m.get_session(sid).status == "idle":
            break
    outs = m.get_session(sid).outputs
    assert outs and Path(outs[-1].path).is_file()
    assert json.loads(outs[-1].code)["nodes"][0]["op"] == "to_grayscale"
