"""SQLite-backed durable store for sessions (Windows, Messages, Outputs)."""

import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

from ...models.dto import Window, Message, GeneratedImage, ExecutionResult, ImageMetadata

DB_PATH = Path("volumes/sessions.db")

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS windows (
                window_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                image_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                current_code TEXT,
                share_token TEXT,
                is_shared INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                response_type TEXT,
                message TEXT NOT NULL,
                code TEXT,
                was_executed INTEGER NOT NULL DEFAULT 0,
                execution_result_json TEXT,
                FOREIGN KEY (window_id) REFERENCES windows (window_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id TEXT NOT NULL,
                image_id TEXT NOT NULL,
                description TEXT NOT NULL,
                path TEXT NOT NULL,
                preview_path TEXT NOT NULL,
                code TEXT NOT NULL,
                source_turn_index INTEGER,
                produced_at TEXT NOT NULL,
                source_iteration INTEGER NOT NULL,
                FOREIGN KEY (window_id) REFERENCES windows (window_id) ON DELETE CASCADE
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
        # Migrate: add image_ids_json column if not already present
        try:
            conn.execute("ALTER TABLE windows ADD COLUMN image_ids_json TEXT")
        except Exception:
            pass  # Column already exists

def save_window(window: Window) -> None:
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO windows 
            (window_id, mode, image_id, created_at, status, current_code, share_token, is_shared, image_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            window.window_id, window.mode, window.image_id, window.created_at.isoformat(),
            window.status, window.current_code, window.share_token, 1 if window.is_shared else 0,
            json.dumps(window.image_ids),
        ))

def save_image(metadata: ImageMetadata) -> None:
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO images
            (image_id, original_filename, shape, dtype, channel_count, size_bytes,
             size_mb, guessed_kind, value_range, path, preview_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metadata.image_id, metadata.original_filename,
            json.dumps(list(metadata.shape)), metadata.dtype,
            metadata.channel_count, metadata.size_bytes, metadata.size_mb,
            metadata.guessed_kind, json.dumps(list(metadata.value_range)),
            metadata.path, metadata.preview_path,
        ))

def load_image(image_id: str) -> Optional[ImageMetadata]:
    with _get_conn() as conn:
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

def load_all_images() -> Dict[str, ImageMetadata]:
    with _get_conn() as conn:
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

def append_message(window_id: str, msg: Message) -> None:
    result_json = json.dumps(msg.execution_result.to_dict()) if msg.execution_result else None
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO messages 
            (window_id, turn_index, role, response_type, message, code, was_executed, execution_result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            window_id, msg.turn_index, msg.role, msg.response_type, msg.message,
            msg.code, 1 if msg.was_executed else 0, result_json
        ))

def append_output(window_id: str, output: GeneratedImage) -> None:
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO outputs 
            (window_id, image_id, description, path, preview_path, code, source_turn_index, produced_at, source_iteration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            window_id, output.image_id, output.description, output.path, output.preview_path,
            output.code, output.source_turn_index, output.produced_at.isoformat(), output.source_iteration
        ))

def list_window_summaries() -> List[Dict[str, Any]]:
    """Returns lightweight projections for the tab bar."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT window_id, mode, image_id, created_at, status, share_token, is_shared
            FROM windows
            ORDER BY created_at ASC
        """).fetchall()
        
        summaries = []
        for row in rows:
            summaries.append({
                "window_id": row["window_id"],
                "mode": row["mode"],
                "image_id": row["image_id"],
                "created_at": row["created_at"],
                "status": row["status"],
                "share_token": row["share_token"],
                "is_shared": bool(row["is_shared"])
            })
        return summaries

def load_window(window_id: str) -> Optional[Window]:
    with _get_conn() as conn:
        w_row = conn.execute("SELECT * FROM windows WHERE window_id = ?", (window_id,)).fetchone()
        if not w_row:
            return None
            
        m_rows = conn.execute("SELECT * FROM messages WHERE window_id = ? ORDER BY turn_index ASC", (window_id,)).fetchall()
        o_rows = conn.execute("SELECT * FROM outputs WHERE window_id = ? ORDER BY id ASC", (window_id,)).fetchall()
        
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
                turn_index=m["turn_index"]
            ))
            
        outputs = []
        for o in o_rows:
            outputs.append(GeneratedImage(
                image_id=o["image_id"],
                window_id=o["window_id"],
                description=o["description"],
                path=o["path"],
                preview_path=o["preview_path"],
                code=o["code"],
                source_turn_index=o["source_turn_index"],
                produced_at=datetime.fromisoformat(o["produced_at"]),
                source_iteration=o["source_iteration"]
            ))
            
        raw_ids = w_row["image_ids_json"]
        image_ids = json.loads(raw_ids) if raw_ids else [w_row["image_id"]]
        return Window(
            window_id=w_row["window_id"],
            mode=w_row["mode"],
            created_at=datetime.fromisoformat(w_row["created_at"]),
            image_id=w_row["image_id"],
            image_ids=image_ids,
            llm_conversation=messages,
            current_code=w_row["current_code"],
            outputs=outputs,
            status=w_row["status"],
            share_token=w_row["share_token"],
            is_shared=bool(w_row["is_shared"])
        )

def delete_window(window_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM windows WHERE window_id = ?", (window_id,))

def resolve_share_token(token: str) -> Optional[str]:
    with _get_conn() as conn:
        row = conn.execute("SELECT window_id FROM windows WHERE share_token = ?", (token,)).fetchone()
        return row["window_id"] if row else None
