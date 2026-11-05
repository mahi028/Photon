"""Lazy LRU cache over SQLite store for Session management.

Thread safety: Uses a threading.Lock around all mutations.
Design: Wraps store.py with a bounded in-memory cache.
"""

from __future__ import annotations

import secrets
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Any, Dict
from collections import OrderedDict

from . import store
from ...models.dto import Session, Message, GeneratedImage, ImageMetadata
from ...config import Config, config

class SessionManager:
    """Lazy LRU cache over SQLite store for sessions."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.RLock()

        # Ensure DB tables exist
        store.init_db()

    def _evict_if_needed(self) -> None:
        """Evict the least recently used session if over limit."""
        while len(self._cache) > Config.MAX_HYDRATED_WINDOWS:
            self._cache.popitem(last=False) # pop LRU (which is at the beginning)

    def _touch(self, session_id: str) -> None:
        """Mark a session as recently used."""
        if session_id in self._cache:
            self._cache.move_to_end(session_id)

    def create_session(self, user_id: str, image_ids: list | None = None,
                        title: str = "Untitled") -> Session:
        session_id = uuid.uuid4().hex
        now = datetime.utcnow()
        sess = Session(
            session_id=session_id,
            user_id=user_id,
            created_at=now,
            updated_at=now,
            title=title,
            image_ids=image_ids or [],
        )

        store.save_session(sess)

        with self._lock:
            self._cache[session_id] = sess
            self._evict_if_needed()

        return sess

    def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve a session from cache, or hydrate from DB."""
        with self._lock:
            if session_id in self._cache:
                cached = self._cache[session_id]
                # Another process (gunicorn worker, MCP server) may have written
                # this session since we cached it; reload rather than serve — or
                # later save_session() over — a stale copy.
                if store.load_session_updated_at(session_id) == cached.updated_at.isoformat():
                    self._touch(session_id)
                    return cached
                del self._cache[session_id]

            # Cache miss, load from DB
            sess = store.load_session(session_id)
            if sess:
                self._cache[session_id] = sess
                self._evict_if_needed()
            return sess

    def invalidate(self, session_id: str) -> None:
        """Drop a session from the cache so the next get re-reads SQLite.

        For processes that share sessions.db with another writer (e.g. the MCP
        server alongside the Flask app): call before reading, so changes the
        other process wrote are picked up.
        """
        with self._lock:
            self._cache.pop(session_id, None)

    def get_session_for_user(self, session_id: str, user_id: str) -> Optional[Session]:
        """Retrieve a session only if it's owned by user_id — the standard
        access-control gate for all session routes."""
        sess = self.get_session(session_id)
        if not sess or sess.user_id != user_id:
            return None
        return sess

    def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Return this user's sessions as lightweight summaries (no hydration)."""
        return store.list_session_summaries(user_id)

    def delete_session(self, session_id: str, user_id: str) -> Optional[List[str]]:
        """Full delete — owner only (see D-020, "smart deletion" per user
        clarification: deleting a session deletes EVERYTHING it owns,
        including its source images, not just generated outputs).

        Returns the list of image_ids actually deleted from disk+DB, or None
        if the session doesn't exist / isn't owned by user_id. An image is
        skipped (kept) if another session — this user's or anyone else's —
        still references it via image_ids (images aren't exclusive to one
        session, see add_image/remove_image), so deleting this session never
        breaks a sibling session that shares an image.

        Sharing no longer blocks deletion (D-019): a share link deep-clones on
        open, so deleting the original never affects clones already made from
        it. Deleting the session cascades its messages, outputs, and
        share_links rows (ON DELETE CASCADE).
        """
        with self._lock:
            sess = self.get_session_for_user(session_id, user_id)
            if not sess:
                return None

            # Generated outputs: whole directory, unconditionally session-owned.
            output_dir = config.OUTPUTS_DIR / session_id
            shutil.rmtree(output_dir, ignore_errors=True)

            # Source images: only delete ones no other session still needs.
            deleted_image_ids: list[str] = []
            for image_id in sess.image_ids:
                if store.image_referenced_elsewhere(image_id, session_id):
                    continue
                img_meta = store.load_image(image_id)
                if img_meta:
                    Path(img_meta.path).unlink(missing_ok=True)
                    if img_meta.preview_path:
                        Path(img_meta.preview_path).unlink(missing_ok=True)
                store.delete_image(image_id)
                deleted_image_ids.append(image_id)

            store.delete_session(session_id)
            self._cache.pop(session_id, None)
            return deleted_image_ids

    def share_session(self, session_id: str, user_id: str, reusable: bool) -> Optional[str]:
        """Create a new share link for a session, return its token. Owner only.

        Each call creates a NEW link (a session can have several active links,
        e.g. some one-time, some reusable) — unlike the old single-token model.
        """
        sess = self.get_session_for_user(session_id, user_id)
        if not sess:
            return None

        token = secrets.token_urlsafe(32)
        store.create_share_link(
            token=token,
            session_id=session_id,
            created_by=user_id,
            reusable=reusable,
            created_at=datetime.utcnow().isoformat(),
        )
        return token

    def revoke_shares(self, session_id: str, user_id: str) -> bool:
        """Revoke ALL active share links for a session. Owner only."""
        sess = self.get_session_for_user(session_id, user_id)
        if not sess:
            return False
        store.delete_share_links_for_session(session_id)
        return True

    def clone_session(self, source_session_id: str, target_user_id: str) -> Optional[Session]:
        """Deep-clone a session into another user's account, with full file
        duplication (uploads + outputs) — see D-019. Does NOT check ownership
        of the source (that's the point: this is how a share link is consumed
        by someone who isn't the owner); callers must have already validated
        the share token."""
        source = self.get_session(source_session_id)
        if not source:
            return None

        new_id = uuid.uuid4().hex

        # 1. Duplicate uploaded images referenced by this session.
        new_image_ids: list[str] = []
        for img_id in source.image_ids:
            img_meta = store.load_image(img_id)
            if not img_meta:
                continue
            new_img_id = uuid.uuid4().hex
            src_path = Path(img_meta.path)
            dst_path = config.UPLOADS_DIR / f"{new_img_id}{src_path.suffix}"
            shutil.copy2(src_path, dst_path)

            src_preview = Path(img_meta.preview_path)
            dst_preview = config.UPLOADS_DIR / f"{new_img_id}_preview.jpg"
            if src_preview.is_file():
                shutil.copy2(src_preview, dst_preview)

            new_meta = ImageMetadata(
                image_id=new_img_id,
                original_filename=img_meta.original_filename,
                shape=img_meta.shape,
                dtype=img_meta.dtype,
                channel_count=img_meta.channel_count,
                size_bytes=img_meta.size_bytes,
                size_mb=img_meta.size_mb,
                guessed_kind=img_meta.guessed_kind,
                value_range=img_meta.value_range,
                path=str(dst_path),
                preview_path=str(dst_preview) if src_preview.is_file() else "",
            )
            store.save_image(new_meta)
            new_image_ids.append(new_img_id)

        # 2. Create the new session.
        now = datetime.utcnow()
        new_session = Session(
            session_id=new_id,
            user_id=target_user_id,
            created_at=now,
            updated_at=now,
            title=f"Copy of {source.title}",
            image_ids=new_image_ids,
            current_code=source.current_code,
        )
        store.save_session(new_session)

        # 3. Copy all messages (new session_id — the Message dataclass itself
        # carries no session_id, so no field to remap on the object).
        for msg in source.llm_conversation:
            store.append_message(new_id, msg)
            new_session.llm_conversation.append(msg)

        # 4. Copy all outputs with file duplication (image + preview + .py sidecar).
        for output in source.outputs:
            new_output_id = uuid.uuid4().hex
            src_file = Path(output.path)
            dst_dir = config.OUTPUTS_DIR / new_id / "cloned"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_file = dst_dir / src_file.name
            if src_file.is_file():
                shutil.copy2(src_file, dst_file)

            dst_preview = ""
            src_preview = Path(output.preview_path) if output.preview_path else None
            if src_preview and src_preview.is_file():
                dst_preview_path = dst_dir / src_preview.name
                shutil.copy2(src_preview, dst_preview_path)
                dst_preview = str(dst_preview_path)

            src_sidecar = src_file.with_suffix(".py")
            if src_sidecar.is_file():
                shutil.copy2(src_sidecar, dst_dir / src_sidecar.name)

            new_output = GeneratedImage(
                image_id=new_output_id,
                session_id=new_id,
                description=output.description,
                path=str(dst_file),
                preview_path=dst_preview,
                code=output.code,
                source_turn_index=output.source_turn_index,
                produced_at=now,
                source_iteration=output.source_iteration,
            )
            store.append_output(new_id, new_output)
            new_session.outputs.append(new_output)

        with self._lock:
            self._cache[new_id] = new_session
            self._evict_if_needed()

        return new_session

    def rename_session(self, session_id: str, user_id: str, title: str) -> bool:
        sess = self.get_session_for_user(session_id, user_id)
        if not sess:
            return False
        sess.title = title.strip() or "Untitled"
        sess.updated_at = datetime.utcnow()
        store.save_session(sess)
        return True

    def add_image(self, session_id: str, image_id: str) -> Optional[Session]:
        """Attach another image to an existing session."""
        sess = self.get_session(session_id)
        if not sess:
            return None
        if image_id not in sess.image_ids:
            sess.image_ids.append(image_id)
            sess.updated_at = datetime.utcnow()
            store.save_session(sess)
        return sess

    def remove_image(self, session_id: str, image_id: str) -> Optional[Session]:
        sess = self.get_session(session_id)
        if not sess:
            return None
        if image_id in sess.image_ids:
            sess.image_ids.remove(image_id)
            sess.updated_at = datetime.utcnow()
            store.save_session(sess)
        return sess

    def add_message(self, session_id: str, msg: Message) -> None:
        """Append a Message to a session."""
        sess = self.get_session(session_id)
        if sess:
            sess.llm_conversation.append(msg)
            store.append_message(session_id, msg)
            sess.updated_at = datetime.utcnow()
            store.save_session(sess)

    def add_output(self, session_id: str, output: GeneratedImage) -> None:
        """Append a GeneratedImage to a session."""
        sess = self.get_session(session_id)
        if sess:
            sess.outputs.append(output)
            store.append_output(session_id, output)
            sess.updated_at = datetime.utcnow()
            store.save_session(sess)

    def set_status(self, session_id: str, status: str) -> None:
        """Update session status ('idle' | 'running' | 'error')."""
        sess = self.get_session(session_id)
        if sess:
            sess.status = status
            store.save_session(sess)

    def save_manual_code(self, session_id: str, code: str) -> None:
        """Persist editor code for a session (only when Run is clicked)."""
        sess = self.get_session(session_id)
        if sess:
            sess.current_code = code
            sess.updated_at = datetime.utcnow()
            store.save_session(sess)

_registry: Optional[SessionManager] = None

def get_manager() -> SessionManager:
    global _registry
    if _registry is None:
        _registry = SessionManager()
    return _registry
