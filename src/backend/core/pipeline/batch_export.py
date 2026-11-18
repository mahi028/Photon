"""Batch results written to a folder on the server's disk (D-050).

Like a Slurm job's output directory: give a batch an `output_dir` and Photon
writes, as each image finishes,

    <output_dir>/<title>_<batch id[:8]>/
        batch.json            what ran (pipeline source, images), status, counts
        log.txt               one line per image as it finishes
        results.csv           one row per image (result values), on finish
        all_values.csv        every node's values, on finish
        tables/<node.port>.csv per-object tables from every image, on finish
        <idx>_<image name>/<node>_<port>.<ext>   each image's final outputs

The copies are extra: the batch page, CSV and zip downloads still read
Photon's own copy under volumes/.

The folder is the user's choice: per batch (`output_dir` in the UI form, REST
or MCP run_batch), else the user's saved default (batches page / MCP
default_output_dir), else the server's PHOTON_OUTPUT_DIR if set; none of these
= no export. Writes run as the server's OS user, so:
  * Photon's own install and data folders are always refused;
  * each batch writes only inside a NEW folder it creates (<title>_<id8>), so it
    never overwrites existing files;
  * an operator can confine exports with PHOTON_OUTPUT_ROOTS (os.pathsep-
    separated); unset = any absolute folder the server can write.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ...config import config

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class ExportError(ValueError):
    pass


def roots() -> list[Path]:
    raw = os.getenv("PHOTON_OUTPUT_ROOTS", "")
    return [Path(r).expanduser().resolve() for r in raw.split(os.pathsep) if r.strip()]


def default_dir() -> Optional[str]:
    return os.getenv("PHOTON_OUTPUT_DIR", "").strip() or None


def _safe(s: Any, n: int = 80) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s)).strip("._")[:n] or "x"


def check(raw: str, create: bool = True) -> Path:
    """Validate a folder for batch results; returns it resolved (created if asked)."""
    p = Path(raw.strip())
    if not p.is_absolute():
        raise ExportError("output_dir must be an absolute path (e.g. /home/<you>/results).")
    p = p.resolve()
    for name, d in (("data", config.VOLUMES_DIR), ("install", config.ROOT_DIR)):
        d = Path(d).resolve()
        if p == d or p.is_relative_to(d):
            raise ExportError(f"output_dir can't be inside Photon's {name} folder ({d}).")
    allowed = roots()
    if allowed and not any(p == r or p.is_relative_to(r) for r in allowed):
        raise ExportError(f"output_dir is outside the folders this server may write to "
                          f"(PHOTON_OUTPUT_ROOTS: {', '.join(map(str, allowed))}).")
    if p.exists() and not p.is_dir():
        raise ExportError(f"output_dir is not a folder: {p}")
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ExportError(f"Can't create output_dir {p}: {e.strerror or e}")
        if not os.access(p, os.W_OK | os.X_OK):
            raise ExportError(f"output_dir is not writable by the Photon server: {p}")
    return p


def user_default(user_id: str) -> Optional[str]:
    from ..session import store
    return store.get_user_output_dir(user_id) or default_dir()


def set_user_default(user_id: str, raw: Optional[str]) -> Optional[str]:
    """Save (checked, created) or clear (None/"") a user's default folder."""
    from ..session import store
    value = str(check(raw)) if raw and raw.strip() else None
    store.set_user_output_dir(user_id, value)
    return value


def resolve(requested: Optional[str], user_id: Optional[str] = None) -> Optional[Path]:
    """The checked parent folder for a new batch, or None (no export).
    `requested` None falls back to the user's default, then PHOTON_OUTPUT_DIR;
    "" means no export for this batch."""
    if requested is not None and not str(requested).strip():
        return None
    raw = str(requested).strip() if requested else (user_default(user_id) if user_id else default_dir())
    return check(raw) if raw else None


def job_dir(parent: Path, title: str, batch_id: str) -> Path:
    return parent / f"{_safe(title, 60)}_{batch_id[:8]}"


def _lock(batch_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(batch_id, threading.Lock())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


def write_manifest(b: dict, items: Optional[list[dict]] = None) -> None:
    out = Path(b["output_dir"])
    c = b.get("counts") or {}
    data = {"batch_id": b["batch_id"], "title": b["title"], "status": b["status"],
            "source": b["source"], "total": b["total"],
            "counts": {k: c.get(k, 0) for k in ("done", "failed", "cancelled", "running", "pending")},
            "created_at": b["created_at"], "started_at": b.get("started_at"),
            "finished_at": b.get("finished_at"), "graph": b["graph"]}
    if items is not None:
        data["images"] = [{"idx": it["idx"], "filename": it["filename"], "image_id": it["image_id"]}
                          for it in items]
    with _lock(b["batch_id"]):
        out.mkdir(parents=True, exist_ok=True)
        _write_json(out / "batch.json", data)


def log(b: dict, line: str) -> None:
    out = Path(b["output_dir"])
    with _lock(b["batch_id"]):
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "log.txt", "a") as f:
            f.write(f"{_now()}  {line}\n")


def item_dir_name(it: dict) -> str:
    return f"{it['idx']:05d}_{_safe(Path(it['filename'] or it['image_id']).stem)}"


def export_item(b: dict, it: dict, base: Path) -> None:
    """Copy one finished image's outputs and tables; log its line."""
    out = Path(b["output_dir"])
    d = out / item_dir_name(it)
    shutil.rmtree(d, ignore_errors=True)       # a retry replaces the earlier attempt
    n = 0
    for o in it.get("outputs") or []:
        src = base / o["path"]
        if src.is_file():
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / f"{_safe(o['node_id'])}_{_safe(o['port'])}{src.suffix}")
            n += 1
    for t in it.get("tables") or []:
        src = base / t["path"]
        if src.is_file():
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / f"{_safe(t['node_id'])}_{_safe(t['port'])}.csv")
            n += 1
    if it.get("values"):
        d.mkdir(parents=True, exist_ok=True)
        _write_json(d / "values.json", it["values"])
    secs = it.get("seconds")
    msg = (f"[{it['idx'] + 1}/{b['total']}] {it['status']:<9} {it['filename'] or it['image_id']}"
           f"  {secs:.1f}s" if isinstance(secs, (int, float)) else
           f"[{it['idx'] + 1}/{b['total']}] {it['status']:<9} {it['filename'] or it['image_id']}")
    msg += f"  files={n}"
    if it.get("error"):
        msg += "  error: " + str(it["error"]).strip().splitlines()[-1][:300]
    log(b, msg)


def finish(b: dict, items: list[dict], values_csv: str, all_values_csv: str,
           tables: dict[str, str]) -> None:
    out = Path(b["output_dir"])
    with _lock(b["batch_id"]):
        (out / "results.csv").write_text(values_csv)
        (out / "all_values.csv").write_text(all_values_csv)
        if tables:
            (out / "tables").mkdir(exist_ok=True)
            for key, text in tables.items():
                (out / "tables" / f"{_safe(key)}.csv").write_text(text)
    write_manifest(b, items)
    c = b.get("counts") or {}
    log(b, f"batch {b['status']}: done={c.get('done', 0)} failed={c.get('failed', 0)} "
           f"cancelled={c.get('cancelled', 0)}")
