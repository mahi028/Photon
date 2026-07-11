"""Lazy LRU cache over SQLite store for Window management.

Thread safety: Uses a threading.Lock around all mutations.
Design: Wraps store.py with a bounded in-memory cache.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Optional, List, Any, Dict
from collections import OrderedDict

from . import store
from ...models.dto import Window, Message, GeneratedImage
from ...config import Config

class WindowManager:
    """Lazy LRU cache over SQLite store for windows."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, Window] = OrderedDict()
        self._lock = threading.RLock()
        
        # Ensure DB tables exist
        store.init_db()

    def _evict_if_needed(self) -> None:
        """Evict the least recently used window if over limit."""
        while len(self._cache) > Config.MAX_HYDRATED_WINDOWS:
            self._cache.popitem(last=False) # pop LRU (which is at the beginning)

    def _touch(self, window_id: str) -> None:
        """Mark a window as recently used."""
        if window_id in self._cache:
            self._cache.move_to_end(window_id)

    def create_window(self, image_id: str, mode: str, image_ids: list | None = None) -> Window:
        window_id = uuid.uuid4().hex
        window = Window(
            window_id=window_id,
            mode=mode,
            created_at=datetime.utcnow(),
            image_id=image_id,
            image_ids=image_ids or [image_id],
        )
        
        store.save_window(window)
        
        with self._lock:
            self._cache[window_id] = window
            self._evict_if_needed()
            
        return window

    def get_window(self, window_id: str) -> Optional[Window]:
        """Retrieve a window from cache, or hydrate from DB."""
        with self._lock:
            if window_id in self._cache:
                self._touch(window_id)
                return self._cache[window_id]
                
            # Cache miss, load from DB
            window = store.load_window(window_id)
            if window:
                self._cache[window_id] = window
                self._evict_if_needed()
            return window

    def list_windows(self) -> List[Dict[str, Any]]:
        """Return all active windows as lightweight summaries (no hydration)."""
        return store.list_window_summaries()

    def delete_window(self, window_id: str) -> bool:
        """Remove a window from both cache and DB."""
        with self._lock:
            window = self.get_window(window_id)
            if not window:
                return False
                
            if window.is_shared:
                # Per simplified spec: shared windows are never deleted by normal DELETE
                # They must be explicitly unshared first.
                return False
            
            store.delete_window(window_id)
            self._cache.pop(window_id, None)
            return True
            
    def share_window(self, window_id: str) -> Optional[str]:
        """Share a window, return token."""
        window = self.get_window(window_id)
        if not window:
            return None
        
        if not window.share_token:
            window.share_token = uuid.uuid4().hex
            window.is_shared = True
            store.save_window(window)
        return window.share_token
        
    def unshare_window(self, window_id: str) -> bool:
        window = self.get_window(window_id)
        if not window:
            return False
        window.share_token = None
        window.is_shared = False
        store.save_window(window)
        return True

    def add_message(self, window_id: str, msg: Message) -> None:
        """Append a Message to a window."""
        window = self.get_window(window_id)
        if window:
            window.llm_conversation.append(msg)
            store.append_message(window_id, msg)

    def add_output(self, window_id: str, output: GeneratedImage) -> None:
        """Append a GeneratedImage to a window."""
        window = self.get_window(window_id)
        if window:
            window.outputs.append(output)
            store.append_output(window_id, output)

    def set_status(self, window_id: str, status: str) -> None:
        """Update window status ('idle' | 'running' | 'error')."""
        window = self.get_window(window_id)
        if window:
            window.status = status
            store.save_window(window)

    def save_manual_code(self, window_id: str, code: str) -> None:
        """Persist editor code for a manual window (ONLY when run is clicked)."""
        window = self.get_window(window_id)
        if window and window.mode == "manual":
            window.current_code = code
            store.save_window(window)

_registry: Optional[WindowManager] = None

def get_manager() -> WindowManager:
    global _registry
    if _registry is None:
        _registry = WindowManager()
    return _registry
