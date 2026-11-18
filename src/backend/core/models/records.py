"""The `models` table (D-049): every model a user has, one row each.

Two kinds, one record shape:

  kind          made by                       file             runs in
  sklearn       a run ending in ml_save_model  .skops           the sandboxed pipeline child (ml_load_model)
  segmentation  an upload (Models page / MCP)  state_dict       the GPU model sidecar (segment_with_model)

Common columns: model_id, user_id, kind, name, file (relative to
volumes/), size_bytes, sha256, status (ready | checking | failed), error,
spec (kind-specific: an sklearn model's estimator / versions / provenance, a
segmentation model's architecture and preprocessing), info (check results),
created_at, published, deleted_at.

Rules shared by both kinds live here: who may use a model (owner, or anyone
once a published pipeline uses it), deleting (hidden from its owner; the file
is kept while a published pipeline may still load it), publishing.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ...config import config

KINDS = ("sklearn", "segmentation")


def models_dir() -> Path:
    d = config.VOLUMES_DIR / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_file(model_id: str, ext: str) -> tuple[Path, str]:
    """(absolute path, value for the `file` column) for a new model file."""
    p = models_dir() / f"{model_id}{ext}"
    return p, p.relative_to(config.VOLUMES_DIR).as_posix()


def file_path(row: dict) -> Path:
    """Where a model's file is. `file` is relative to volumes/ (rows migrated
    from the old tables keep their old folders)."""
    p = (config.VOLUMES_DIR / row["file"]).resolve()
    if config.VOLUMES_DIR.resolve() not in p.parents:
        raise ValueError("model file outside volumes/")
    return p


def is_id(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 32 and all(c in "0123456789abcdef" for c in s)


def get(model_id: Any, kind: Optional[str] = None) -> Optional[dict]:
    from ..session import store
    row = store.get_model(model_id) if is_id(model_id) else None
    return row if row and (kind is None or row["kind"] == kind) else None


def can_use(user_id: str, row: Optional[dict]) -> bool:
    """Ready, and either the user's own (not deleted) or published."""
    return bool(row) and row["status"] == "ready" and (
        row["published"] or (row["user_id"] == user_id and not row["deleted_at"]))


def public(row: dict) -> dict:
    out = {k: row[k] for k in ("model_id", "kind", "name", "size_bytes", "status", "error",
                               "spec", "info", "created_at", "published")}
    out["description"] = row["spec"].get("description", "")
    if row["kind"] == "sklearn":                    # flat fields the editor / older clients use
        for k in ("estimator", "versions", "session_id", "pipeline_title", "summary"):
            out[k] = row["spec"].get(k)
    return out


def list_for(user_id: str, kind: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    from ..session import store
    return [public(r) for r in store.list_models(user_id, kind)
            if status is None or r["status"] == status]


def delete(user_id: str, model_id: str) -> bool:
    from ..session import store
    row = get(model_id)
    if not row or row["user_id"] != user_id or row["deleted_at"]:
        return False
    store.update_model(model_id, deleted_at=datetime.utcnow().isoformat())
    if not row["published"]:                        # a published pipeline may still load it
        try:
            file_path(row).unlink(missing_ok=True)
        except ValueError:
            pass
    return True


def check_publishable(user_id: str, model_ids: list[str], kind: str, label: str) -> list[str]:
    """Each model must be ready and the publisher's own, or already published
    (a fork of someone's pipeline keeps working). Raises ValueError."""
    for mid in model_ids:
        row = get(mid, kind)
        if not row or row["status"] != "ready" or not (
                row["published"] or (row["user_id"] == user_id and not row["deleted_at"])):
            raise ValueError(f"{label} '{mid}' isn't one of yours (or isn't ready), so it can't be published.")
    return model_ids


def mark_published(model_ids: list[str]) -> None:
    from ..session import store
    for mid in model_ids:
        store.update_model(mid, published=1)
