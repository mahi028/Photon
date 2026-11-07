"""SQLite-backed durable store for sessions (Sessions, Messages, Outputs, Users).

A Session unifies the old mode-locked Window: one record holds both the LLM
conversation and the manual-editor code, toggled in the UI (see DECISIONS D-018).
"""

import sqlite3
from contextlib import closing
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

from ...models.dto import Session, Message, GeneratedImage, ExecutionResult, ImageMetadata
from ...config import config

# Absolute path — a relative path here silently creates a fresh, empty DB
# whenever the app is launched from a different working directory.
DB_PATH = config.VOLUMES_DIR / "sessions.db"

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                email_verified INTEGER NOT NULL DEFAULT 0,
                password_hash TEXT,
                provider TEXT,
                provider_id TEXT,
                display_name TEXT,
                avatar_url TEXT,
                created_at TEXT NOT NULL,
                last_login TEXT NOT NULL,
                UNIQUE(provider, provider_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                current_code TEXT,
                image_ids_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                response_type TEXT,
                message TEXT NOT NULL,
                code TEXT,
                was_executed INTEGER NOT NULL DEFAULT 0,
                execution_result_json TEXT,
                internal INTEGER NOT NULL DEFAULT 0,
                pipeline_json TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id) ON DELETE CASCADE
            )
        """)
        # Additive migration for DBs created before v3 (no wipe needed).
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN pipeline_json TEXT")
        except Exception:
            pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                image_id TEXT NOT NULL,
                description TEXT NOT NULL,
                path TEXT NOT NULL,
                preview_path TEXT NOT NULL,
                code TEXT NOT NULL,
                source_turn_index INTEGER,
                produced_at TEXT NOT NULL,
                source_iteration INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS images (
                image_id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                shape TEXT NOT NULL,
                dtype TEXT NOT NULL,
                channel_count INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                size_mb REAL NOT NULL,
                guessed_kind TEXT NOT NULL,
                value_range TEXT NOT NULL,
                path TEXT NOT NULL,
                preview_path TEXT NOT NULL
            )
        """)
        # Share links: source of truth for sharing (replaces the old
        # share_token/is_shared columns on sessions — see D-019). A session
        # can have multiple links over time; each is independently one-time
        # or reusable. Token is a high-entropy secret, not a guessable id —
        # only rows actually created via POST /sessions/<id>/share exist here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_links (
                token TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_by TEXT NOT NULL,
                reusable INTEGER NOT NULL DEFAULT 0,
                used_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id) ON DELETE CASCADE,
                FOREIGN KEY (created_by) REFERENCES users (user_id)
            )
        """)
        # --- v3 pipelines (see photon_v3_plan.md) ---
        # user_id NULL == built-in preset: visible to everyone, read-only,
        # cloned into a user's library to edit.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipelines (
                pipeline_id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT NOT NULL DEFAULT 'Untitled pipeline',
                description TEXT NOT NULL DEFAULT '',
                graph_json TEXT NOT NULL DEFAULT '{"nodes":[],"edges":[]}',
                preset_slug TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        # LLM/user-authored nodes. code is AST-checked (sandbox checker) at
        # SAVE time; the runner execs it inside the resource-limited run.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_nodes (
                custom_node_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                code TEXT NOT NULL,
                ports_json TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '[]',
                doc TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        # API keys for programmatic access (MCP server). Only a SHA-256 hash of
        # the key is stored; the plaintext is shown to the user once at creation.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                expires_at TEXT,
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id)")
        # Published pipelines (D-035): an instance-wide, immutable, versioned
        # snapshot of a user's pipeline. Each version freezes the graph AND the
        # code of every custom node it uses, so runs by other users never
        # depend on the author's live (editable, per-user) library.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_pipelines (
                published_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                source_pipeline_id TEXT UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                latest_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                unpublished_at TEXT,
                FOREIGN KEY (owner_id) REFERENCES users (user_id) ON DELETE CASCADE,
                FOREIGN KEY (source_pipeline_id) REFERENCES pipelines (pipeline_id) ON DELETE SET NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_pipeline_versions (
                published_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                graph_json TEXT NOT NULL,
                custom_nodes_json TEXT NOT NULL DEFAULT '{}',
                changelog TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                PRIMARY KEY (published_id, version),
                FOREIGN KEY (published_id) REFERENCES published_pipelines (published_id) ON DELETE CASCADE
            )
        """)
        _seed_presets(conn)
        # --- additive migrations (run after every CREATE TABLE) ---
        # Uploader of each image (D-037). NULL for images from before this
        # column: those are reachable only through a session that lists them.
        try:
            conn.execute("ALTER TABLE images ADD COLUMN owner_id TEXT")
        except Exception:
            pass
        # When status last changed: lets a 'running' left behind by a crashed
        # process expire instead of locking the session forever.
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN status_at TEXT")
        except Exception:
            pass
        # Library versions a published version was made with (D-041), e.g.
        # {"scikit-image": "0.25.2", "numpy": "2.2.0"}. NULL for older rows.
        try:
            conn.execute("ALTER TABLE published_pipeline_versions ADD COLUMN env_json TEXT")
        except Exception:
            pass


def _seed_presets(conn) -> None:
    """Insert built-in preset pipelines that aren't in the DB yet (idempotent)."""
    from ..pipeline.presets import PRESETS
    now = datetime.utcnow().isoformat()
    for preset in PRESETS:
        conn.execute("""
            INSERT INTO pipelines (pipeline_id, user_id, title, description,
                                   graph_json, preset_slug, created_at, updated_at)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(preset_slug) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                graph_json = excluded.graph_json,
                updated_at = excluded.updated_at
        """, (f"preset_{preset['slug']}", preset["title"], preset["description"],
              json.dumps(preset["graph"]), preset["slug"], now, now))


def save_session(sess: Session) -> None:
    # UPSERT, NOT "INSERT OR REPLACE": REPLACE is implemented as DELETE+INSERT,
    # and with PRAGMA foreign_keys=ON the DELETE cascades — silently wiping the
    # session's messages and outputs on every status update (see D-012).
    # status is NOT updated here: only claim_session_run/set_session_status
    # write it, so a stale cached copy can't overwrite another process's run.
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO sessions
            (session_id, user_id, title, created_at, updated_at, status,
             current_code, image_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                title = excluded.title,
                updated_at = excluded.updated_at,
                current_code = excluded.current_code,
                image_ids_json = excluded.image_ids_json
        """, (
            sess.session_id, sess.user_id, sess.title, sess.created_at.isoformat(),
            sess.updated_at.isoformat(), sess.status, sess.current_code,
            json.dumps(sess.image_ids),
        ))

def save_image(metadata: ImageMetadata, owner_id: Optional[str] = None) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT OR REPLACE INTO images
            (image_id, original_filename, shape, dtype, channel_count, size_bytes,
             size_mb, guessed_kind, value_range, path, preview_path, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metadata.image_id, metadata.original_filename,
            json.dumps(list(metadata.shape)), metadata.dtype,
            metadata.channel_count, metadata.size_bytes, metadata.size_mb,
            metadata.guessed_kind, json.dumps(list(metadata.value_range)),
            metadata.path, metadata.preview_path, owner_id,
        ))


def user_can_access_image(user_id: str, image_id: str) -> bool:
    """Uploader, or the owner of any session that lists the image (covers
    images from before owner_id existed and images cloned via share links)."""
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT owner_id FROM images WHERE image_id = ?", (image_id,)).fetchone()
        if row is None:
            return False
        if row["owner_id"] == user_id:
            return True
        rows = conn.execute(
            "SELECT image_ids_json FROM sessions WHERE user_id = ? AND image_ids_json LIKE ?",
            (user_id, f"%{image_id}%")).fetchall()
        return any(image_id in json.loads(r["image_ids_json"] or "[]") for r in rows)


def image_id_for_upload_file(filename: str) -> Optional[str]:
    """volumes/uploads/<image_id>[_preview].<ext> -> image_id if registered."""
    stem = Path(filename).name.split(".", 1)[0]
    stem = stem[:-len("_preview")] if stem.endswith("_preview") else stem
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT image_id FROM images WHERE image_id = ?", (stem,)).fetchone()
        return row["image_id"] if row else None

def load_image(image_id: str) -> Optional[ImageMetadata]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM images WHERE image_id = ?", (image_id,)).fetchone()
        if not row:
            return None
        return ImageMetadata(
            image_id=row["image_id"],
            original_filename=row["original_filename"],
            shape=tuple(json.loads(row["shape"])),
            dtype=row["dtype"],
            channel_count=row["channel_count"],
            size_bytes=row["size_bytes"],
            size_mb=row["size_mb"],
            guessed_kind=row["guessed_kind"],
            value_range=tuple(json.loads(row["value_range"])),
            path=row["path"],
            preview_path=row["preview_path"],
        )

def delete_image(image_id: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("DELETE FROM images WHERE image_id = ?", (image_id,))

def image_referenced_elsewhere(image_id: str, exclude_session_id: str) -> bool:
    """True if any session OTHER than exclude_session_id still lists this image
    (images aren't exclusive to one session — see POST /sessions/<id>/images).
    Used by smart deletion to avoid deleting a file a sibling session still needs.
    """
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute(
            "SELECT image_ids_json FROM sessions WHERE session_id != ?",
            (exclude_session_id,),
        ).fetchall()
        for row in rows:
            if image_id in json.loads(row["image_ids_json"] or "[]"):
                return True
        return False

def load_all_images() -> Dict[str, ImageMetadata]:
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("SELECT * FROM images").fetchall()
        result = {}
        for row in rows:
            result[row["image_id"]] = ImageMetadata(
                image_id=row["image_id"],
                original_filename=row["original_filename"],
                shape=tuple(json.loads(row["shape"])),
                dtype=row["dtype"],
                channel_count=row["channel_count"],
                size_bytes=row["size_bytes"],
                size_mb=row["size_mb"],
                guessed_kind=row["guessed_kind"],
                value_range=tuple(json.loads(row["value_range"])),
                path=row["path"],
                preview_path=row["preview_path"],
            )
        return result

def append_message(session_id: str, msg: Message) -> None:
    result_json = json.dumps(msg.execution_result.to_dict()) if msg.execution_result else None
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO messages
            (session_id, turn_index, role, response_type, message, code, was_executed,
             execution_result_json, internal, pipeline_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, msg.turn_index, msg.role, msg.response_type, msg.message,
            msg.code, 1 if msg.was_executed else 0, result_json,
            1 if msg.internal else 0,
            json.dumps(msg.pipeline) if msg.pipeline else None,
        ))

def append_output(session_id: str, output: GeneratedImage) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO outputs
            (session_id, image_id, description, path, preview_path, code, source_turn_index, produced_at, source_iteration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, output.image_id, output.description, output.path, output.preview_path,
            output.code, output.source_turn_index, output.produced_at.isoformat(), output.source_iteration
        ))

def list_session_summaries(user_id: str) -> List[Dict[str, Any]]:
    """Returns lightweight, user-scoped projections for the sidebar — no hydration."""
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("""
            SELECT session_id, title, created_at, updated_at, status, image_ids_json
            FROM sessions
            WHERE user_id = ?
            ORDER BY updated_at DESC
        """, (user_id,)).fetchall()

        summaries = []
        for row in rows:
            image_ids = json.loads(row["image_ids_json"] or "[]")
            summaries.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "status": row["status"],
                "image_id": image_ids[0] if image_ids else None,
                "image_count": len(image_ids),
            })
        return summaries

def load_session_updated_at(session_id: str) -> Optional[str]:
    """Cheap staleness probe for the SessionManager cache (one indexed row)."""
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT updated_at FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
        return row["updated_at"] if row else None


def load_session_version(session_id: str) -> Optional[tuple]:
    """(updated_at, status): what the SessionManager cache compares against.
    status is included because it changes without bumping updated_at."""
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT updated_at, status FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
        return (row["updated_at"], row["status"]) if row else None


def claim_session_run(session_id: str, now: str, stale_before: str) -> bool:
    """Atomically idle -> running, across processes (Flask workers, MCP
    server). A 'running' older than stale_before counts as abandoned."""
    with closing(_get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE sessions SET status = 'running', status_at = ? WHERE session_id = ? "
            "AND (status != 'running' OR status_at IS NULL OR status_at < ?)",
            (now, session_id, stale_before))
        return cur.rowcount == 1


def set_session_status(session_id: str, status: str, now: str) -> None:
    """Status only: never rewrites the rest of the row (which another process
    may have changed since this one cached it)."""
    with closing(_get_conn()) as conn, conn:
        conn.execute("UPDATE sessions SET status = ?, status_at = ? WHERE session_id = ?",
                     (status, now, session_id))


def load_session(session_id: str) -> Optional[Session]:
    with closing(_get_conn()) as conn, conn:
        s_row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not s_row:
            return None

        m_rows = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY turn_index ASC", (session_id,)).fetchall()
        o_rows = conn.execute("SELECT * FROM outputs WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()

        messages = []
        for m in m_rows:
            exec_res = None
            if m["execution_result_json"]:
                data = json.loads(m["execution_result_json"])
                exec_res = ExecutionResult(
                    stdout=data.get("stdout", ""),
                    stderr=data.get("stderr", ""),
                    traceback=data.get("traceback"),
                    time_taken_seconds=data.get("time_taken_seconds", 0.0),
                    file_exists=data.get("file_exists", False),
                    output_path=data.get("output_path"),
                    timed_out=data.get("timed_out", False)
                )

            messages.append(Message(
                role=m["role"],
                response_type=m["response_type"],
                message=m["message"],
                code=m["code"],
                was_executed=bool(m["was_executed"]),
                execution_result=exec_res,
                turn_index=m["turn_index"],
                internal=bool(m["internal"]),
                pipeline=json.loads(m["pipeline_json"]) if m["pipeline_json"] else None,
            ))

        outputs = []
        for o in o_rows:
            outputs.append(GeneratedImage(
                image_id=o["image_id"],
                session_id=o["session_id"],
                description=o["description"],
                path=o["path"],
                preview_path=o["preview_path"],
                code=o["code"],
                source_turn_index=o["source_turn_index"],
                produced_at=datetime.fromisoformat(o["produced_at"]),
                source_iteration=o["source_iteration"]
            ))

        image_ids = json.loads(s_row["image_ids_json"] or "[]")
        return Session(
            session_id=s_row["session_id"],
            user_id=s_row["user_id"],
            title=s_row["title"],
            created_at=datetime.fromisoformat(s_row["created_at"]),
            updated_at=datetime.fromisoformat(s_row["updated_at"]),
            image_ids=image_ids,
            llm_conversation=messages,
            current_code=s_row["current_code"],
            outputs=outputs,
            status=s_row["status"],
        )

def delete_session(session_id: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


# ---------------------------------------------------------------------------
# Share links (Phase 4 — share-as-clone; see D-019)
# ---------------------------------------------------------------------------

def create_share_link(token: str, session_id: str, created_by: str, reusable: bool, created_at: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO share_links (token, session_id, created_by, reusable, used_count, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (token, session_id, created_by, 1 if reusable else 0, created_at))

def get_share_link(token: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM share_links WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

def increment_share_link_usage(token: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("UPDATE share_links SET used_count = used_count + 1 WHERE token = ?", (token,))

def delete_share_links_for_session(session_id: str) -> None:
    """Revoke all active share links for a session (owner-initiated revoke;
    also happens implicitly via ON DELETE CASCADE when the session itself is
    deleted)."""
    with closing(_get_conn()) as conn, conn:
        conn.execute("DELETE FROM share_links WHERE session_id = ?", (session_id,))


# ---------------------------------------------------------------------------
# Users (Phase 2 — username/password always available, Google OAuth optional)
# ---------------------------------------------------------------------------

def create_user(
    user_id: str,
    username: str,
    email: str,
    password_hash: Optional[str],
    provider: Optional[str],
    provider_id: Optional[str],
    display_name: str,
    avatar_url: str,
    created_at: str,
) -> None:
    """Create a user. `password_hash` is NULL for Google-only accounts;
    `provider`/`provider_id` are NULL for password-only accounts."""
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO users (user_id, username, email, password_hash,
                               provider, provider_id, display_name, avatar_url,
                               created_at, last_login)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, username, email, password_hash, provider, provider_id,
              display_name, avatar_url, created_at, created_at))


def find_user_by_username_or_email(identifier: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?",
            (identifier, identifier.lower())
        ).fetchone()
        return dict(row) if row else None


def find_user_by_provider(provider: str, provider_id: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM users WHERE provider = ? AND provider_id = ?",
            (provider, provider_id)
        ).fetchone()
        return dict(row) if row else None


def update_user_login(user_id: str, last_login: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE user_id = ?",
            (last_login, user_id)
        )


# ---------------------------------------------------------------------------
# Pipelines + custom nodes (v3 — see photon_v3_plan.md)
# ---------------------------------------------------------------------------

def _pipeline_row_to_dict(row) -> Dict[str, Any]:
    return {
        "pipeline_id": row["pipeline_id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "description": row["description"],
        "graph": json.loads(row["graph_json"]),
        "is_preset": row["user_id"] is None,
        "preset_slug": row["preset_slug"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_pipelines(user_id: str) -> List[Dict[str, Any]]:
    """The user's own pipelines plus all built-in presets."""
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("""
            SELECT * FROM pipelines
            WHERE user_id = ? OR user_id IS NULL
            ORDER BY (user_id IS NULL), updated_at DESC
        """, (user_id,)).fetchall()
        return [_pipeline_row_to_dict(r) for r in rows]


def load_pipeline(pipeline_id: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM pipelines WHERE pipeline_id = ?",
                           (pipeline_id,)).fetchone()
        return _pipeline_row_to_dict(row) if row else None


def save_pipeline(pipeline_id: str, user_id: str, title: str, description: str,
                  graph: dict, created_at: str, updated_at: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO pipelines (pipeline_id, user_id, title, description,
                                   graph_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                graph_json = excluded.graph_json,
                updated_at = excluded.updated_at
        """, (pipeline_id, user_id, title, description, json.dumps(graph),
              created_at, updated_at))


def delete_pipeline(pipeline_id: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("DELETE FROM pipelines WHERE pipeline_id = ? AND user_id IS NOT NULL",
                     (pipeline_id,))


def _custom_node_row_to_dict(row) -> Dict[str, Any]:
    return {
        "custom_node_id": row["custom_node_id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "code": row["code"],
        "ports": json.loads(row["ports_json"]),
        "params": json.loads(row["params_json"]),
        "doc": row["doc"],
        "created_at": row["created_at"],
    }


def list_custom_nodes(user_id: str) -> List[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute(
            "SELECT * FROM custom_nodes WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)).fetchall()
        return [_custom_node_row_to_dict(r) for r in rows]


def load_custom_node(custom_node_id: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM custom_nodes WHERE custom_node_id = ?",
                           (custom_node_id,)).fetchone()
        return _custom_node_row_to_dict(row) if row else None


def find_custom_node_by_name(user_id: str, name: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM custom_nodes WHERE user_id = ? AND name = ?",
                           (user_id, name)).fetchone()
        return _custom_node_row_to_dict(row) if row else None


def save_custom_node(custom_node_id: str, user_id: str, name: str, code: str,
                     ports: dict, params: list, doc: str, created_at: str) -> None:
    """Upsert by (user_id, name) so regenerating a node replaces it in place."""
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO custom_nodes (custom_node_id, user_id, name, code,
                                      ports_json, params_json, doc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET
                code = excluded.code,
                ports_json = excluded.ports_json,
                params_json = excluded.params_json,
                doc = excluded.doc
        """, (custom_node_id, user_id, name, code, json.dumps(ports),
              json.dumps(params), doc, created_at))


def delete_custom_node(custom_node_id: str, user_id: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("DELETE FROM custom_nodes WHERE custom_node_id = ? AND user_id = ?",
                     (custom_node_id, user_id))


# ---------------------------------------------------------------------------
# Published pipelines
# ---------------------------------------------------------------------------

_PUB_SELECT = """
    SELECT p.*, u.username AS owner_username, u.display_name AS owner_display_name,
           v.published_at AS latest_published_at, v.graph_json AS latest_graph_json
    FROM published_pipelines p
    JOIN users u ON u.user_id = p.owner_id
    JOIN published_pipeline_versions v
      ON v.published_id = p.published_id AND v.version = p.latest_version
"""


def _pub_row_to_dict(row) -> Dict[str, Any]:
    graph = json.loads(row["latest_graph_json"])
    return {
        "published_id": row["published_id"],
        "owner_id": row["owner_id"],
        "owner_username": row["owner_username"],
        "owner_display_name": row["owner_display_name"],
        "source_pipeline_id": row["source_pipeline_id"],
        "title": row["title"],
        "description": row["description"],
        "latest_version": row["latest_version"],
        "node_count": len(graph.get("nodes") or []),
        "uses_custom_nodes": any(n.get("custom_node_id") for n in graph.get("nodes") or []),
        "ops": [n.get("op") or "custom" for n in graph.get("nodes") or []],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest_published_at": row["latest_published_at"],
        "unpublished_at": row["unpublished_at"],
    }


def list_published_pipelines(include_unpublished_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """All live publications, newest first. include_unpublished_of=user_id also
    returns that user's unpublished ones (so owners can see and re-publish)."""
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute(_PUB_SELECT + """
            WHERE p.unpublished_at IS NULL OR p.owner_id = ?
            ORDER BY p.updated_at DESC
        """, (include_unpublished_of or "",)).fetchall()
        return [_pub_row_to_dict(r) for r in rows]


def load_published_pipeline(published_id: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute(_PUB_SELECT + " WHERE p.published_id = ?",
                           (published_id,)).fetchone()
        return _pub_row_to_dict(row) if row else None


def find_published_by_source(pipeline_id: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute(_PUB_SELECT + " WHERE p.source_pipeline_id = ?",
                           (pipeline_id,)).fetchone()
        return _pub_row_to_dict(row) if row else None


def list_published_versions(published_id: str) -> List[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("""
            SELECT version, title, changelog, published_at FROM published_pipeline_versions
            WHERE published_id = ? ORDER BY version DESC
        """, (published_id,)).fetchall()
        return [dict(r) for r in rows]


def load_published_version(published_id: str, version: int) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("""
            SELECT * FROM published_pipeline_versions WHERE published_id = ? AND version = ?
        """, (published_id, version)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["graph"] = json.loads(d.pop("graph_json"))
        d["custom_nodes"] = json.loads(d.pop("custom_nodes_json"))
        d["environment"] = json.loads(d.pop("env_json", None) or "{}")
        return d


def add_published_version(published_id: str, owner_id: str, source_pipeline_id: str,
                          title: str, description: str, graph: dict,
                          custom_nodes: dict, changelog: str, now: str,
                          environment: Optional[dict] = None) -> int:
    """Create the publication if needed and append the next version, atomically.
    Re-publishing also clears unpublished_at. Returns the new version number."""
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT latest_version FROM published_pipelines WHERE published_id = ?",
                           (published_id,)).fetchone()
        version = (row["latest_version"] + 1) if row else 1
        if row:
            conn.execute("""
                UPDATE published_pipelines SET title = ?, description = ?, latest_version = ?,
                       updated_at = ?, unpublished_at = NULL
                WHERE published_id = ?
            """, (title, description, version, now, published_id))
        else:
            conn.execute("""
                INSERT INTO published_pipelines (published_id, owner_id, source_pipeline_id,
                    title, description, latest_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (published_id, owner_id, source_pipeline_id, title, description,
                  version, now, now))
        conn.execute("""
            INSERT INTO published_pipeline_versions (published_id, version, title, description,
                graph_json, custom_nodes_json, changelog, published_at, env_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (published_id, version, title, description, json.dumps(graph),
              json.dumps(custom_nodes), changelog, now, json.dumps(environment or {})))
        return version


def set_published_unpublished(published_id: str, owner_id: str, unpublished_at: Optional[str]) -> bool:
    with closing(_get_conn()) as conn, conn:
        cur = conn.execute("""
            UPDATE published_pipelines SET unpublished_at = ?
            WHERE published_id = ? AND owner_id = ?
        """, (unpublished_at, published_id, owner_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def create_api_key(key_id: str, user_id: str, name: str, key_hash: str, prefix: str,
                   created_at: str, expires_at: Optional[str]) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("""
            INSERT INTO api_keys (key_id, user_id, name, key_hash, prefix, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (key_id, user_id, name, key_hash, prefix, created_at, expires_at))


def list_api_keys(user_id: str) -> List[Dict[str, Any]]:
    """A user's keys, newest first. Never includes the hash."""
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("""
            SELECT key_id, name, prefix, created_at, last_used_at, expires_at, revoked_at
            FROM api_keys WHERE user_id = ? ORDER BY created_at DESC
        """, (user_id,)).fetchall()
        return [dict(r) for r in rows]


def find_api_key_by_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)).fetchone()
        return dict(row) if row else None


def revoke_api_key(key_id: str, user_id: str, revoked_at: str) -> bool:
    """Owner-scoped revoke. Returns False if the key isn't the user's or is already revoked."""
    with closing(_get_conn()) as conn, conn:
        cur = conn.execute("""
            UPDATE api_keys SET revoked_at = ?
            WHERE key_id = ? AND user_id = ? AND revoked_at IS NULL
        """, (revoked_at, key_id, user_id))
        return cur.rowcount > 0


def touch_api_key(key_id: str, used_at: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE key_id = ?", (used_at, key_id))


def load_user(user_id: str) -> Optional[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> List[Dict[str, Any]]:
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute(
            "SELECT user_id, username, email, provider, display_name, created_at, last_login "
            "FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def set_user_password(user_id: str, password_hash: str) -> None:
    with closing(_get_conn()) as conn, conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (password_hash, user_id))


def recent_outputs_for_user(user_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    """A user's newest outputs across their sessions: {path, code, produced_at}.
    `code` is the provenance (for pipeline runs, the graph JSON)."""
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute("""
            SELECT o.path, o.code, o.produced_at
            FROM outputs o JOIN sessions s ON s.session_id = o.session_id
            WHERE s.user_id = ?
            ORDER BY o.produced_at DESC, o.id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
