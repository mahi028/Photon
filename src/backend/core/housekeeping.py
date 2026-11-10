"""Disk housekeeping and per-user quotas (D-048).

Sweeps (hourly, in the process that serves requests / the MCP server):
  * tmp_exec/     run scratch dirs, worker job dirs, zip downloads older than
                  TMP_MAX_AGE (each run also deletes its own; this catches
                  crashes and runs from before that existed);
  * model_ipc/    .npy hand-over files older than an hour;
  * model_grants/ expired per-run model grants;
  * batches       finished batches older than PHOTON_BATCH_RETENTION_DAYS
                  (0 = keep forever, the default): results and rows; the
                  uploaded source images stay.

Quota: PHOTON_USER_QUOTA_GB (0 = unlimited, the default) caps what one user
stores: uploaded images, session outputs, batch outputs, saved and uploaded
models. Checked before anything new is written (uploads, runs, batches, model
uploads); usage is measured from the database plus the output folders and
cached for a minute, so a check is cheap.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import config

logger = logging.getLogger(__name__)

TMP_MAX_AGE = 6 * 3600
IPC_MAX_AGE = 3600
SWEEP_INTERVAL = 3600
_USAGE_TTL = 60.0


class QuotaError(Exception):
    status = 507


def quota_bytes() -> int:
    try:
        return int(float(os.getenv("PHOTON_USER_QUOTA_GB", "0")) * (1 << 30))
    except ValueError:
        return 0


def retention_days() -> int:
    try:
        return max(0, int(os.getenv("PHOTON_BATCH_RETENTION_DAYS", "0")))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Usage + quota
# ---------------------------------------------------------------------------

_usage_cache: dict[str, tuple[float, dict]] = {}
_usage_lock = threading.Lock()


def _dir_bytes(p: Path) -> int:
    total = 0
    stack = [p]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def usage(user_id: str, fresh: bool = False) -> dict:
    """Bytes this user stores, by kind, plus the quota (0 = unlimited)."""
    now = time.monotonic()
    with _usage_lock:
        hit = _usage_cache.get(user_id)
        if hit and not fresh and now - hit[0] < _USAGE_TTL:
            return hit[1]
    from .session import store
    from contextlib import closing
    with closing(store._get_conn()) as conn:
        q = lambda sql: int(conn.execute(sql, (user_id,)).fetchone()[0] or 0)
        images = q("SELECT SUM(size_bytes) FROM images WHERE owner_id = ?")
        models = q("SELECT SUM(size_bytes) FROM models WHERE user_id = ? AND deleted_at IS NULL")
        sessions = [r[0] for r in conn.execute("SELECT session_id FROM sessions WHERE user_id = ?", (user_id,))]
        batches = [r[0] for r in conn.execute("SELECT batch_id FROM batches WHERE user_id = ?", (user_id,))]
    outputs = sum(_dir_bytes(config.OUTPUTS_DIR / s) for s in sessions)
    batch_bytes = sum(_dir_bytes(config.OUTPUTS_DIR / "batches" / b) for b in batches)
    u = {"images": images, "session_outputs": outputs, "batch_outputs": batch_bytes, "models": models}
    u["total"] = sum(u.values())
    u["quota"] = quota_bytes()
    with _usage_lock:
        _usage_cache[user_id] = (now, u)
    return u


def forget_usage(user_id: Optional[str] = None) -> None:
    with _usage_lock:
        if user_id:
            _usage_cache.pop(user_id, None)
        else:
            _usage_cache.clear()


def _gb(n: int) -> str:
    return f"{n / (1 << 30):.2f} GB"


def check_quota(user_id: Optional[str], adding: int = 0) -> None:
    """Raise QuotaError if the user is over quota (or would be after `adding`)."""
    cap = quota_bytes()
    if not cap or not user_id:
        return
    u = usage(user_id)
    if u["total"] + adding > cap:
        raise QuotaError(f"Storage quota reached: {_gb(u['total'])} of {_gb(cap)} used"
                         + (f" (this needs {_gb(adding)} more)" if adding else "")
                         + ". Delete sessions, batches or models to free space.")
    if adding:
        forget_usage(user_id)        # the write is about to happen: measure again next time


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

def _older_than(p: Path, age: float) -> bool:
    try:
        return time.time() - p.stat().st_mtime > age
    except OSError:
        return False


def sweep_tmp() -> dict:
    """Stale scratch, IPC hand-over files and expired grants."""
    n = {"tmp": 0, "ipc": 0, "grants": 0}
    tmp = Path(config.TMP_EXEC_DIR)
    if tmp.is_dir():
        for p in tmp.iterdir():
            if _older_than(p, TMP_MAX_AGE):
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                n["tmp"] += 1
    ipc = Path(config.MODEL_IPC_DIR)
    if ipc.is_dir():
        for p in ipc.glob("*.npy"):
            if _older_than(p, IPC_MAX_AGE):
                p.unlink(missing_ok=True)
                n["ipc"] += 1
    grants = config.VOLUMES_DIR / "model_grants"
    if grants.is_dir():
        import json
        for p in grants.iterdir():
            try:
                expired = float(json.loads(p.read_text()).get("expires", 0)) < time.time()
            except (OSError, ValueError):
                expired = _older_than(p, 24 * 3600)
            if expired:
                p.unlink(missing_ok=True)
                n["grants"] += 1
    return n


def sweep_batches(days: Optional[int] = None) -> int:
    """Delete finished batches older than `days` (default: the retention
    setting; 0 = keep). Returns how many."""
    days = retention_days() if days is None else days
    if days <= 0:
        return 0
    from .pipeline import batch
    from .session import store
    from contextlib import closing
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with closing(store._get_conn()) as conn:
        rows = conn.execute("""SELECT batch_id, user_id FROM batches
                               WHERE status NOT IN ('queued', 'running')
                               AND COALESCE(finished_at, created_at) < ?""", (cutoff,)).fetchall()
    for bid, uid in rows:
        try:
            batch.delete(uid, bid)
            forget_usage(uid)
        except Exception:                        # noqa: BLE001
            logger.exception("could not delete old batch %s", bid)
    return len(rows)


def sweep_all() -> dict:
    out = sweep_tmp()
    out["batches"] = sweep_batches()
    return out


_started = False
_start_lock = threading.Lock()


def start() -> None:
    """Run the sweeps now and then hourly, on a daemon thread (once per process)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def loop():
        while True:
            try:
                r = sweep_all()
                if any(r.values()):
                    logger.info("housekeeping: %s", r)
            except Exception:                    # noqa: BLE001
                logger.exception("housekeeping sweep failed")
            time.sleep(SWEEP_INTERVAL)

    threading.Thread(target=loop, name="housekeeping", daemon=True).start()
