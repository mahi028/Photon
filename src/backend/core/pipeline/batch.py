"""Batch runs (D-046): one pipeline over many images, in the background.

A batch freezes its graph (and custom nodes) at creation, then runs one
sandboxed pipeline per image on a small shared thread pool
(PHOTON_BATCH_WORKERS, default 2; each run is its own sandbox process).
Every image gets a row: status, error, time, its non-image values (numbers,
thresholds...) and, because batch runs export them, its full tables
(regionprops_table and the like) as CSV. Results download as one values CSV
(a row per image, a column per node.port), one CSV per table (rows from every
image, tagged with the image), and a zip of everything.

Outputs live in volumes/outputs/batches/<batch_id>/<index>/, not in a
session: a 500-image batch would swamp the session view.

A batch survives restarts: rows record the process running it, and a process
that starts up takes over (atomically) any batch whose process is gone and
carries on with the images not done yet. Cancel stops after the images
already running.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ...config import config
from ..image.thumbnail import generate_thumbnail
from ..session import store
from . import publishing, saved, subgraph

MAX_ITEMS = int(os.getenv("PHOTON_BATCH_MAX_IMAGES", "2000"))
MAX_ACTIVE_PER_USER = 3
TERMINAL = ("done", "failed", "cancelled")

_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()
_ctx: dict[str, dict] = {}          # batch_id -> expanded graph etc., for this process
_ctx_lock = threading.Lock()


class BatchError(Exception):
    def __init__(self, message: str, status: int = 400, issues: list | None = None):
        super().__init__(message)
        self.status = status
        self.issues = issues or []


def _workers() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            n = max(1, int(os.getenv("PHOTON_BATCH_WORKERS", "2")))
            _pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="batch")
        return _pool


def batch_dir(batch_id: str) -> Path:
    return config.OUTPUTS_DIR / "batches" / batch_id


def _now() -> str:
    return datetime.utcnow().isoformat()


def _image_meta(image_id: str):
    from ...api.upload_routes import get_image_registry
    reg = get_image_registry()
    meta = reg.get(image_id)
    if meta is None:
        meta = store.load_image(image_id)
        if meta is not None:
            reg[image_id] = meta
    return meta


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------

def resolve_source(user_id: str, pipeline_id: str | None = None, published_id: str | None = None,
                   version: int | None = None, graph: dict | None = None) -> tuple[dict, dict, str, dict]:
    """(graph, custom nodes, title, source) from exactly one of the three."""
    if sum(bool(x) for x in (pipeline_id, published_id, graph)) != 1:
        raise BatchError("Pass exactly one of pipeline_id, published_id or graph.")
    if published_id:
        try:
            g, customs, title, prov = publishing.resolve_for_run(user_id, published_id, version)
        except publishing.PublishError as e:
            raise BatchError(str(e), e.status)
        return g, customs, title, {"published_id": published_id, "version": prov["version"]}
    customs = {cn["custom_node_id"]: cn for cn in store.list_custom_nodes(user_id)}
    if pipeline_id:
        p = store.load_pipeline(pipeline_id)
        if not p or (p["user_id"] is not None and p["user_id"] != user_id):
            raise BatchError("Pipeline not found.", 404)
        return p["graph"], customs, p["title"], {"pipeline_id": pipeline_id}
    return graph, customs, "ad-hoc graph", {"graph": True}


def images_for(user_id: str, image_ids: list[str] | None = None,
               session_ids: list[str] | None = None) -> list[dict]:
    """[{image_id, filename}] the user may use, in order, without duplicates."""
    from ..session.manager import get_manager
    ids: list[str] = list(image_ids or [])
    for sid in session_ids or []:
        sess = get_manager().get_session_for_user(sid, user_id)
        if not sess:
            raise BatchError(f"Session {sid} not found.", 404)
        ids.extend(sess.image_ids)
    out, seen = [], set()
    for iid in ids:
        if iid in seen:
            continue
        seen.add(iid)
        meta = _image_meta(iid) if store.user_can_access_image(user_id, iid) else None
        if meta is None:
            raise BatchError(f"Image {iid} not found.", 404)
        out.append({"image_id": iid, "filename": meta.original_filename})
    return out


def create(user_id: str, images: list[dict], title: str | None = None, **source) -> dict:
    """Validate, store and start a batch. `source`: pipeline_id | published_id
    (+version) | graph. Returns the batch."""
    if not images:
        raise BatchError("No images.")
    if len(images) > MAX_ITEMS:
        raise BatchError(f"{len(images)} images; a batch takes at most {MAX_ITEMS}.")
    active = [b for b in store.list_batches(user_id, 50) if b["status"] in ("queued", "running")]
    if len(active) >= MAX_ACTIVE_PER_USER:
        raise BatchError(f"You already have {len(active)} batches running; wait or cancel one.", 429)
    graph, customs, src_title, src = resolve_source(user_id, **source)
    issues = subgraph.validate(graph, customs, user_id)
    if issues:
        raise BatchError("Pipeline is invalid", 400, [i.to_dict() for i in issues])
    # Custom nodes are frozen with the batch: only the ones it uses.
    used = {n.get("custom_node_id") for n in graph.get("nodes") or []}
    frozen = {cid: {k: cn[k] for k in ("custom_node_id", "name", "code", "ports", "params", "doc")
                    if k in cn}
              for cid, cn in customs.items() if cid in used}
    bid = uuid.uuid4().hex
    row = {"batch_id": bid, "user_id": user_id, "title": (title or src_title or "batch").strip()[:200],
           "source": src, "graph": graph, "custom_nodes": frozen, "status": "queued",
           "created_at": _now()}
    store.create_batch(row, images)
    start(bid)
    return public(store.get_batch(bid))


# ---------------------------------------------------------------------------
# Many images at once
# ---------------------------------------------------------------------------

ZIP_MAX_ENTRIES = 2000
ZIP_MAX_TOTAL = 4 << 30          # uncompressed bytes per archive


def ingest_files(user_id: str, files: list[tuple[str, Any]]) -> tuple[list[dict], list[dict]]:
    """[(filename, bytes | binary file object)] -> (images, skipped). Images are
    registered as uploads the user owns; .zip archives are unpacked."""
    from ...api.upload_routes import get_image_registry
    from ..image.ingest import IngestError, ingest_image_bytes
    from pathlib import PurePosixPath
    images: list[dict] = []
    skipped: list[dict] = []
    cap = config.MAX_UPLOAD_BYTES

    def one(data: bytes, name: str):
        try:
            meta = ingest_image_bytes(data, name, user_id)
        except IngestError as e:
            skipped.append({"filename": name, "reason": str(e)})
            return
        get_image_registry()[meta.image_id] = meta
        images.append({"image_id": meta.image_id, "filename": meta.original_filename})

    for name, src in files:
        name = PurePosixPath(name or "image").name
        if not name.lower().endswith(".zip"):
            one(src if isinstance(src, bytes) else src.read(), name)
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(src) if isinstance(src, bytes) else src)
        except zipfile.BadZipFile:
            skipped.append({"filename": name, "reason": "not a valid zip"})
            continue
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_ENTRIES or sum(i.file_size for i in infos) > ZIP_MAX_TOTAL:
            skipped.append({"filename": name, "reason": f"zip too large (max {ZIP_MAX_ENTRIES} files)"})
            continue
        for i in sorted(infos, key=lambda i: i.filename):
            inner = PurePosixPath(i.filename).name
            if inner.startswith(".") or "__MACOSX" in i.filename:
                continue
            if i.file_size > cap:
                skipped.append({"filename": inner, "reason": "file too large"})
                continue
            with zf.open(i) as fh:
                data = fh.read(cap + 1)          # the declared size can lie
            if len(data) > cap:
                skipped.append({"filename": inner, "reason": "file too large"})
                continue
            one(data, inner)
    return images, skipped


def _page_csv(text: str, offset: int, limit: int) -> dict:
    lines = text.splitlines()
    header, body = lines[:1], lines[1:]
    offset, limit = max(0, offset), max(1, min(limit, 5000))
    page = body[offset:offset + limit]
    return {"csv": "\n".join(header + page), "rows": len(page), "total_rows": len(body),
            "offset": offset, "next_offset": offset + len(page) if offset + len(page) < len(body) else None}


def values_page(user_id: str, batch_id: str, offset: int = 0, limit: int = 1000) -> dict:
    return _page_csv(values_csv(user_id, batch_id), offset, limit)


def table_page(user_id: str, batch_id: str, key: str, offset: int = 0, limit: int = 1000) -> dict:
    return _page_csv(table_csv(user_id, batch_id, key), offset, limit)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def start(batch_id: str) -> None:
    """Run this batch's pending images in this process."""
    b = store.get_batch(batch_id)
    if not b:
        return
    store.update_batch(batch_id, status="running", runner_pid=os.getpid(),
                       started_at=b["started_at"] or _now())
    try:
        run_graph, customs, outer = subgraph.expand(b["graph"], b["user_id"], b["custom_nodes"])
    except subgraph.SubgraphError as e:
        for it in store.list_batch_items(batch_id, ["pending", "running"]):
            store.update_batch_item(batch_id, it["idx"], status="failed", error=str(e))
        _finish_if_done(batch_id)
        return
    with _ctx_lock:
        _ctx[batch_id] = {"graph": run_graph, "customs": customs, "outer": outer,
                          "user_id": b["user_id"], "title": b["title"]}
    store.reset_running_batch_items(batch_id)
    pending = store.list_batch_items(batch_id, ["pending"])
    if not pending:
        _finish_if_done(batch_id)
        return
    pool = _workers()
    for it in pending:
        pool.submit(_run_item_safe, batch_id, it["idx"], it["image_id"])


def _run_item_safe(batch_id: str, idx: int, image_id: str) -> None:
    try:
        _run_item(batch_id, idx, image_id)
    except Exception as e:                       # never lose an item to a bug
        store.update_batch_item(batch_id, idx, status="failed", error=f"internal error: {e}")
    finally:
        _finish_if_done(batch_id)


def _run_item(batch_id: str, idx: int, image_id: str) -> None:
    from ..sandbox import backend as sandbox_backend
    b = store.get_batch(batch_id)
    if not b or b["runner_pid"] != os.getpid():
        return                                   # deleted, or taken over elsewhere
    if b["cancel_requested"]:
        store.update_batch_item(batch_id, idx, status="cancelled")
        return
    ctx = _ctx.get(batch_id)
    meta = _image_meta(image_id)
    if not ctx or meta is None:
        store.update_batch_item(batch_id, idx, status="failed", error="image not found")
        return
    store.update_batch_item(batch_id, idx, status="running")
    t0 = time.time()
    out_dir = batch_dir(batch_id) / f"{idx:05d}"
    shutil.rmtree(out_dir, ignore_errors=True)
    node_dir = out_dir / "nodes"
    try:
        assets = saved.stage(ctx["graph"], ctx["user_id"], image_id, (meta.shape[1], meta.shape[0]))
    except saved.AssetError as e:
        store.update_batch_item(batch_id, idx, status="failed", error=str(e), seconds=time.time() - t0)
        return
    result = sandbox_backend.run_pipeline(
        ctx["graph"], meta.path, str(out_dir), str(node_dir), custom_nodes=ctx["customs"],
        input_mode="RGB" if meta.channel_count >= 3 else "L", assets=assets, export_tables=True)
    preview = _main_preview(result, out_dir)
    shutil.rmtree(node_dir, ignore_errors=True)  # other node previews aren't kept
    if ctx["outer"]:
        subgraph.collapse(result, ctx["outer"])
    saved.register_saved(ctx["user_id"], result.get("saved_models") or [], None, ctx["title"])

    base = batch_dir(batch_id)
    outputs = []
    for f in result.get("finals") or []:
        p = Path(f["path"])
        prev = p.with_name(p.stem + "_preview.jpg")
        try:
            generate_thumbnail(p, prev, max_long_edge=256)
        except Exception:
            prev = None
        outputs.append({"node_id": f["node_id"], "port": f["port"],
                        "path": p.relative_to(base).as_posix(),
                        "preview": prev.relative_to(base).as_posix() if prev and prev.exists() else None})
    tables = [{"node_id": t["node_id"], "port": t["port"], "rows": t.get("rows"),
               "path": Path(t["path"]).relative_to(base).as_posix()}
              for t in result.get("tables") or [] if t.get("path")]
    store.update_batch_item(batch_id, idx, status="failed" if result.get("error") else "done",
                            error=(result.get("error") or None) and str(result["error"])[-2000:],
                            seconds=round(time.time() - t0, 2), values=result.get("values") or {},
                            outputs=outputs, tables=tables,
                            preview={"node_id": preview[0], "path": preview[1].relative_to(base).as_posix()}
                            if preview else None)


def _main_preview(result: dict, out_dir: Path) -> Optional[tuple[str, Path]]:
    """(node, 256-px thumbnail) of the last node, in run order, that made an
    image. Shown when every image a graph makes feeds another node (e.g. labels
    -> regionprops), so there is no final output to show, and on failures."""
    previews = result.get("previews") or {}
    done = [e["node_id"] for e in result.get("events") or [] if e.get("phase") == "done"]
    for nid in reversed(done):
        info = previews.get(nid)
        if not info or not Path(info["path"]).is_file():
            continue
        dst = out_dir / "main_preview.jpg"
        try:
            generate_thumbnail(Path(info["path"]), dst, max_long_edge=256)
            return nid, dst
        except Exception:
            return None
    return None


def _input_preview_url(image_id: str) -> Optional[str]:
    meta = _image_meta(image_id)
    if not meta or not meta.preview_path:
        return None
    try:
        rel = Path(meta.preview_path).relative_to(config.UPLOADS_DIR)
    except ValueError:
        return None
    return f"/previews/uploads/{rel.as_posix()}"


def _finish_if_done(batch_id: str) -> None:
    b = store.get_batch(batch_id)
    if not b or b["status"] not in ("queued", "running"):
        return
    c = b["counts"]
    if c.get("pending") or c.get("running"):
        return
    store.update_batch(batch_id, status="cancelled" if b["cancel_requested"] else "done",
                       finished_at=_now())
    with _ctx_lock:
        _ctx.pop(batch_id, None)


def cancel(user_id: str, batch_id: str) -> dict:
    b = _owned(user_id, batch_id)
    if b["status"] in ("queued", "running"):
        store.update_batch(batch_id, cancel_requested=1)
        if b["runner_pid"] != os.getpid() and not _alive(b["runner_pid"]):
            for it in store.list_batch_items(batch_id, ["pending", "running"]):
                store.update_batch_item(batch_id, it["idx"], status="cancelled")
            _finish_if_done(batch_id)
    return public(store.get_batch(batch_id))


def retry_failed(user_id: str, batch_id: str) -> dict:
    """Run the failed and cancelled images again (e.g. after fixing a timeout)."""
    b = _owned(user_id, batch_id)
    if b["status"] in ("queued", "running"):
        raise BatchError("Batch is still running.", 409)
    again = store.list_batch_items(batch_id, ["failed", "cancelled"])
    if not again:
        raise BatchError("Nothing to retry.")
    for it in again:
        store.update_batch_item(batch_id, it["idx"], status="pending", error=None)
    store.update_batch(batch_id, status="queued", cancel_requested=0, finished_at=None)
    start(batch_id)
    return public(store.get_batch(batch_id))


def delete(user_id: str, batch_id: str) -> None:
    b = _owned(user_id, batch_id)
    if b["status"] in ("queued", "running"):
        raise BatchError("Cancel the batch before deleting it.", 409)
    store.delete_batch(batch_id, user_id)
    shutil.rmtree(batch_dir(batch_id), ignore_errors=True)


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def recover() -> int:
    """Take over batches whose process died (call at startup). Returns how many."""
    n = 0
    for b in store.active_batches():
        pid = b["runner_pid"]
        if pid == os.getpid() or _alive(pid):
            continue
        if store.claim_batch(b["batch_id"], pid, os.getpid()):
            threading.Thread(target=start, args=(b["batch_id"],), daemon=True).start()
            n += 1
    return n


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _owned(user_id: str, batch_id: str) -> dict:
    b = store.get_batch(batch_id)
    if not b or b["user_id"] != user_id:
        raise BatchError("Batch not found.", 404)
    return b


def public(b: dict) -> dict:
    c = b.get("counts") or {}
    return {"batch_id": b["batch_id"], "title": b["title"], "status": b["status"],
            "source": b["source"], "total": b["total"], "done": c.get("done", 0),
            "failed": c.get("failed", 0), "cancelled": c.get("cancelled", 0),
            "running": c.get("running", 0), "pending": c.get("pending", 0),
            "created_at": b["created_at"], "started_at": b["started_at"],
            "finished_at": b["finished_at"], "cancel_requested": b["cancel_requested"]}


def get(user_id: str, batch_id: str, with_items: bool = True) -> dict:
    b = _owned(user_id, batch_id)
    out = public(b)
    if with_items:
        items = store.list_batch_items(batch_id)
        for it in items:
            it["input_preview"] = _input_preview_url(it["image_id"])
        out["items"] = items
        out["columns"] = value_columns(items)
        out["tables"] = table_keys(items)
    return out


def _flat(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v
    return json.dumps(v)[:500]


def value_columns(items: list[dict]) -> list[str]:
    """node.port columns for the per-image values table. Values exported as
    tables (per-object rows) are left out: they have their own CSV."""
    tabled = {f"{t['node_id']}.{t['port']}" for it in items for t in it.get("tables") or []}
    cols: list[str] = []
    for it in items:
        for nid, ports in (it.get("values") or {}).items():
            if "/" in nid:
                continue                     # inner nodes of a pipeline node: reported on it
            for port in ports:
                key = f"{nid}.{port}"
                if key not in cols and key not in tabled:
                    cols.append(key)
    return cols


def values_csv(user_id: str, batch_id: str) -> str:
    _owned(user_id, batch_id)
    items = store.list_batch_items(batch_id)
    cols = value_columns(items)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["index", "filename", "image_id", "status", "seconds", "error", *cols])
    for it in items:
        vals = it.get("values") or {}
        row = []
        for key in cols:
            nid, port = key.rsplit(".", 1) if "." in key else (key, "")
            row.append(_flat((vals.get(nid) or {}).get(port)))
        w.writerow([it["idx"], it["filename"], it["image_id"], it["status"], it["seconds"],
                    (it["error"] or "")[:300], *row])
    return buf.getvalue()


def table_keys(items: list[dict]) -> list[dict]:
    keys: dict[str, dict] = {}
    for it in items:
        for t in it.get("tables") or []:
            k = f"{t['node_id']}.{t['port']}"
            d = keys.setdefault(k, {"key": k, "node_id": t["node_id"], "port": t["port"],
                                    "images": 0, "rows": 0})
            d["images"] += 1
            d["rows"] += t.get("rows") or 0
    return list(keys.values())


def table_csv(user_id: str, batch_id: str, key: str) -> str:
    """One table from every image, concatenated, with the image in front."""
    _owned(user_id, batch_id)
    base = batch_dir(batch_id)
    header: list[str] = []
    rows: list[tuple[dict, dict]] = []
    found = False
    for it in store.list_batch_items(batch_id):
        for t in it.get("tables") or []:
            if f"{t['node_id']}.{t['port']}" != key:
                continue
            p = (base / t["path"]).resolve()
            if base.resolve() not in p.parents or not p.is_file():
                continue
            found = True
            with open(p, newline="") as f:
                r = csv.DictReader(f)
                for h in r.fieldnames or []:
                    if h not in header:
                        header.append(h)
                rows.extend((it, row) for row in r)
    if not found:
        raise BatchError("No such table.", 404)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["index", "filename", "image_id", *header])
    for it, row in rows:
        w.writerow([it["idx"], it["filename"], it["image_id"], *[row.get(h, "") for h in header]])
    return buf.getvalue()


def output_file(user_id: str, batch_id: str, rel: str) -> Path:
    _owned(user_id, batch_id)
    base = batch_dir(batch_id).resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        raise BatchError("Not found.", 404)
    return p


def zip_all(user_id: str, batch_id: str) -> Path:
    """values.csv, every table CSV and every output image, in one zip (a temp file)."""
    b = get(user_id, batch_id)
    base = batch_dir(batch_id)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=config.TMP_EXEC_DIR)
    os.close(fd)
    safe = lambda s: "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s))[:80]
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("values.csv", values_csv(user_id, batch_id))
        for t in b["tables"]:
            zf.writestr(f"tables/{safe(t['key'])}.csv", table_csv(user_id, batch_id, t["key"]))
        for it in b["items"]:
            stem = safe(Path(it["filename"] or it["image_id"]).stem)
            for o in it["outputs"]:
                p = base / o["path"]
                if p.is_file():
                    zf.write(p, f"outputs/{it['idx']:05d}_{stem}_{safe(o['node_id'])}_{safe(o['port'])}{p.suffix}")
    return Path(tmp)
