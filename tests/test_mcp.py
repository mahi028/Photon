"""MCP server over streamable HTTP: API-key auth, ownership, SSRF, runs."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import closing

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
import mcp.client.streamable_http as sh  # noqa: E402

from src.backend.core.session import store  # noqa: E402

pytestmark = pytest.mark.slow
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_connect = getattr(sh, "streamable_http_client", None) or getattr(sh, "streamablehttp_client")


@pytest.fixture(scope="module")
def mcp_url():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = subprocess.Popen([sys.executable, os.path.join(REPO, "src/mcp_server/__main__.py"),
                            "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
                           env=os.environ.copy(), stderr=subprocess.DEVNULL, cwd="/")
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(90):
        try:
            httpx.get(url, timeout=1)
            break
        except Exception:
            time.sleep(1)
    yield url
    srv.terminate()
    srv.wait(10)


def _call(url, key, steps):
    """Open one MCP session as `key` and run steps(session) (async)."""
    async def go():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=120) as hc:
            async with _connect(url, http_client=hc) as st, ClientSession(st[0], st[1]) as s:
                await s.initialize()
                return await steps(s)
    return asyncio.run(go())


def _text(r):
    return next((c.text for c in r.content if c.type == "text"), "")


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def _raw_status(url, auth):
    h = {"Accept": "application/json, text/event-stream"}
    if auth:
        h["Authorization"] = auth
    return httpx.post(url, json=INIT, headers=h, timeout=10).status_code


def test_api_key_lifecycle(mcp_url, make_user, app):
    c1, _, _ = make_user()
    c2, _, _ = make_user()
    assert app.test_client().post("/api/keys", json={"name": "x"}).status_code == 401
    k1 = c1.post("/api/keys", json={"name": "k1", "expires_in_days": 30}).get_json()
    k2 = c2.post("/api/keys", json={"name": "k2"}).get_json()
    assert not any("key_hash" in k or "key" in k for k in c1.get("/api/keys").get_json())
    assert c2.delete(f"/api/keys/{k1['key_id']}").status_code == 404

    for bad in (None, "Bearer pk_nope", "Basic abc"):
        assert _raw_status(mcp_url, bad) == 401

    async def make(s):
        return json.loads(_text(await s.call_tool("create_session", {"title": "via key"})))["session_id"]
    sid1 = _call(mcp_url, k1["key"], make)
    assert sid1 in {s["session_id"] for s in c1.get("/api/sessions").get_json()}
    assert sid1 not in {s["session_id"] for s in c2.get("/api/sessions").get_json()}

    assert c1.delete(f"/api/keys/{k1['key_id']}").status_code == 204
    assert _raw_status(mcp_url, f"Bearer {k1['key']}") == 401
    with closing(store._get_conn()) as conn, conn:
        conn.execute("UPDATE api_keys SET expires_at = '2000-01-01T00:00:00' WHERE key_id = ?", (k2["key_id"],))
    assert _raw_status(mcp_url, f"Bearer {k2['key']}") == 401


def test_key_scopes(mcp_url, make_user):
    """read < run < write (D-043): each tool checks the key's scope."""
    c, _, _ = make_user()
    assert c.post("/api/keys", json={"name": "x", "scope": "admin"}).status_code == 400
    keys = {sc: c.post("/api/keys", json={"name": sc, "scope": sc}).get_json() for sc in ("read", "run", "write")}
    assert {k["scope"] for k in c.get("/api/keys").get_json()} == {"read", "run", "write"}
    assert c.post("/api/keys", json={"name": "legacy"}).get_json()["scope"] == "write"

    async def probe(s):
        out = {"list": not (await s.call_tool("list_sessions", {})).is_error}
        r = await s.call_tool("create_session", {"title": "scoped"})
        out["create"] = not r.is_error
        out["create_msg"] = _text(r)
        g = {"nodes": [{"id": "g", "op": "to_grayscale"}],
             "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}
        out["save"] = not (await s.call_tool("save_pipeline", {"title": "s", "graph": g})).is_error
        out["models"] = not (await s.call_tool("list_models", {})).is_error
        r = await s.call_tool("save_labels", {"image_id": "0" * 32, "data_base64": ""})
        out["labels_msg"] = _text(r)
        return out
    got = {sc: _call(mcp_url, k["key"], probe) for sc, k in keys.items()}
    assert [got[sc]["list"] for sc in keys] == [True, True, True]
    assert [got[sc]["create"] for sc in keys] == [False, True, True]
    assert [got[sc]["save"] for sc in keys] == [False, False, True]
    assert "needs a 'run' API key" in got["read"]["create_msg"]
    assert all(got[sc]["models"] for sc in keys)
    assert "needs a 'run' API key" in got["read"]["labels_msg"]
    assert "not found" in got["run"]["labels_msg"]


def test_tools_upload_run_download_delete(mcp_url, make_user, upload, disk_png):
    c, _, _ = make_user()
    other, _, _ = make_user()
    foreign_img = upload(other)
    key = c.post("/api/keys", json={"name": "tools"}).get_json()["key"]

    async def steps(s):
        res = {}
        for u in ["http://127.0.0.1:8889/", "http://169.254.169.254/latest/meta-data",
                  "http://10.0.0.1/a.png", "file:///etc/passwd", "http://[::1]/a.png"]:
            r = await s.call_tool("upload_image", {"url": u})
            res.setdefault("ssrf_refused", []).append(r.is_error)
        r = await s.call_tool("upload_image", {"path": "/etc/passwd"})
        res["path_outside"] = r.is_error
        r = await s.call_tool("create_session", {"title": "x", "image_ids": [foreign_img]})
        res["foreign_image_refused"] = r.is_error
        import base64
        b64 = base64.b64encode(open(disk_png, "rb").read()).decode()
        sid = json.loads(_text(await s.call_tool("create_session", {"title": "tools"})))["session_id"]
        r = await s.call_tool("upload_image", {"data_base64": b64, "filename": "d.png", "session_id": sid})
        res["upload_b64"] = not r.is_error
        graph = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}}],
                 "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}
        r = await s.call_tool("run_pipeline", {"session_id": sid, "graph": graph})
        run = json.loads(_text(r))
        res["run"] = not r.is_error and run["ok"]
        oid = run["outputs"][0]["output_id"]
        r = await s.call_tool("download_output", {"session_id": sid, "output_id": oid})
        blob = next(x for x in r.content if x.type == "resource").resource
        res["download_png"] = base64.b64decode(blob.blob)[:4] == b"\x89PNG"
        r = await s.call_tool("delete_session", {"session_id": sid, "confirm_title": "wrong"})
        res["delete_wrong_title_refused"] = r.is_error
        r = await s.call_tool("delete_session", {"session_id": sid, "confirm_title": "tools"})
        res["deleted"] = not r.is_error
        res["sid"] = sid
        return res

    res = _call(mcp_url, key, steps)
    assert all(res.pop("ssrf_refused"))
    sid = res.pop("sid")
    assert all(res.values()), res
    assert c.get(f"/api/sessions/{sid}").status_code == 404


def test_async_run_and_poll(mcp_url, make_user, upload):
    c, _, _ = make_user()
    other, _, _ = make_user()
    sid = c.post("/api/sessions", json={"image_ids": [upload(c)]}).get_json()["session_id"]
    key = c.post("/api/keys", json={"name": "async"}).get_json()["key"]
    other_key = other.post("/api/keys", json={"name": "other"}).get_json()["key"]
    graph = {"nodes": [{"id": "g", "op": "to_grayscale", "params": {}}],
             "edges": [{"from": ["input", "image"], "to": ["g", "image"]}]}

    async def start(s):
        r = await s.call_tool("run_pipeline", {"session_id": sid, "graph": graph, "wait": False})
        job = json.loads(_text(r))
        assert job["status"] == "running"
        for _ in range(120):
            r = await s.call_tool("get_run", {"job_id": job["job_id"]})
            st = json.loads(_text(r))
            if st["status"] == "done":
                return job["job_id"], st, any(x.type == "image" for x in r.content)
            await asyncio.sleep(0.5)
        raise TimeoutError

    job_id, st, has_preview = _call(mcp_url, key, start)
    assert st["ok"] and st["outputs"] and st["done"] == st["total"] == 1 and has_preview

    async def peek(s):
        return (await s.call_tool("get_run", {"job_id": job_id})).is_error
    assert _call(mcp_url, other_key, peek), "another user must not see the job"


def test_library_ops_over_mcp(mcp_url, make_user):
    c, _, _ = make_user()
    key = c.post("/api/keys", json={"name": "lib"}).get_json()["key"]

    async def steps(s):
        cat = json.loads(_text(await s.call_tool("list_ops", {"query": "multiotsu"})))
        spec = json.loads(_text(await s.call_tool("describe_op", {"name": "lib:cv2.threshold"})))
        denied = await s.call_tool("describe_op", {"name": "lib:skimage.io.imread"})
        graph = {"nodes": [{"id": "t", "op": "lib:skimage.filters.threshold_multiotsu"}],
                 "edges": [{"from": ["input", "image"], "to": ["t", "image"]}]}
        v = json.loads(_text(await s.call_tool("validate_pipeline", {"graph": graph})))
        return cat, spec, denied.is_error, v

    cat, spec, denied, v = _call(mcp_url, key, steps)
    assert cat["library_ops"][0]["name"] == "lib:skimage.filters.threshold_multiotsu"
    assert [o["name"] for o in spec["outputs"]] == ["retval", "dst"]
    assert denied and v["valid"], v
