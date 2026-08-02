"""ModelManager — the only thing allowed to hold a loaded model.

Enforces the project's VRAM rule structurally rather than by convention:

- A model is loaded ONLY when `acquire()` is first called for it.
- A background reaper unloads any model idle longer than
  MODEL_IDLE_TIMEOUT_SECONDS (default 300s = the 5 minute rule).
- A model currently in use is never evicted (refcount guard) — an idle timer
  firing mid-inference would be a miserable heisenbug.
- Total declared VRAM is capped by MODEL_VRAM_BUDGET_MB; loading something new
  LRU-evicts unreferenced models to make room, and fails loudly rather than
  OOM-ing the GPU if it still doesn't fit.

Op authors never call load/unload — `@register_model_op` injects an already
loaded handle. See guard.py for why that isn't merely a suggestion.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from ...config import config
from . import registry

logger = logging.getLogger(__name__)


class ModelLoadError(RuntimeError):
    """Raised when a model cannot be loaded (unknown, or won't fit in budget)."""


@dataclass
class _Entry:
    name: str
    handle: object
    vram_mb: int
    refcount: int = 0
    last_used: float = field(default_factory=time.monotonic)
    loaded_at: float = field(default_factory=time.monotonic)


class ModelManager:
    def __init__(self, idle_timeout: Optional[float] = None,
                 vram_budget_mb: Optional[int] = None,
                 device: Optional[str] = None,
                 reaper_interval: Optional[float] = None,
                 start_reaper: bool = True) -> None:
        self.idle_timeout = (config.MODEL_IDLE_TIMEOUT_SECONDS
                             if idle_timeout is None else idle_timeout)
        self.reaper_interval = (config.MODEL_REAPER_INTERVAL_SECONDS
                                if reaper_interval is None else reaper_interval)
        self._device = device
        self._budget_mb = vram_budget_mb
        self._lock = threading.RLock()
        self._loaded: dict[str, _Entry] = {}
        self._load_counts: dict[str, int] = {}   # observability + tests
        self._stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None
        if start_reaper:
            self.start_reaper()

    # -- device / budget --------------------------------------------------

    @property
    def device(self) -> str:
        if self._device:
            return self._device
        configured = (config.MODEL_DEVICE or "auto").lower()
        if configured != "auto":
            self._device = configured
            return self._device
        try:
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            self._device = "cpu"
        return self._device

    @property
    def vram_budget_mb(self) -> int:
        if self._budget_mb:
            return self._budget_mb
        if config.MODEL_VRAM_BUDGET_MB:
            self._budget_mb = config.MODEL_VRAM_BUDGET_MB
            return self._budget_mb
        # Derive from the device: leave headroom for activations, not just weights.
        try:
            import torch
            if torch.cuda.is_available():
                total_mb = torch.cuda.get_device_properties(0).total_memory // (1024 ** 2)
                self._budget_mb = int(total_mb * 0.5)
                return self._budget_mb
        except Exception:
            pass
        self._budget_mb = 8192   # CPU fallback: bound host RAM use too
        return self._budget_mb

    # -- core API ---------------------------------------------------------

    @contextmanager
    def acquire(self, name: str):
        """Yield a loaded model handle, holding a reference for the duration.

        Loads on first use. The refcount held here is what stops the reaper
        from unloading a model that's mid-inference.
        """
        entry = self._get_or_load(name)
        with self._lock:
            entry.refcount += 1
        try:
            yield entry.handle
        finally:
            with self._lock:
                entry.refcount -= 1
                entry.last_used = time.monotonic()

    def _get_or_load(self, name: str) -> _Entry:
        with self._lock:
            entry = self._loaded.get(name)
            if entry is not None:
                entry.last_used = time.monotonic()
                return entry

            spec = registry.get_model_spec(name)
            if spec is None:
                known = ", ".join(sorted(registry.all_models())) or "(none registered)"
                raise ModelLoadError(f"Unknown model '{name}'. Registered: {known}")

            self._make_room_for(spec.vram_mb, exclude=name)

            logger.info("Loading model %s (~%d MB) on %s", spec.name, spec.vram_mb, self.device)
            t0 = time.monotonic()
            handle = spec.loader(self.device)
            logger.info("Loaded model %s in %.1fs", spec.name, time.monotonic() - t0)

            entry = _Entry(name=name, handle=handle, vram_mb=spec.vram_mb)
            self._loaded[name] = entry
            self._load_counts[name] = self._load_counts.get(name, 0) + 1
            return entry

    def _make_room_for(self, needed_mb: int, exclude: str) -> None:
        """LRU-evict unreferenced models until `needed_mb` fits in the budget."""
        budget = self.vram_budget_mb
        if needed_mb > budget:
            raise ModelLoadError(
                f"Model needs ~{needed_mb} MB but the whole budget is {budget} MB. "
                f"Raise MODEL_VRAM_BUDGET_MB or use a smaller model.")

        def used() -> int:
            return sum(e.vram_mb for e in self._loaded.values())

        if used() + needed_mb <= budget:
            return

        candidates = sorted(
            (e for e in self._loaded.values() if e.refcount == 0 and e.name != exclude),
            key=lambda e: e.last_used)
        for entry in candidates:
            logger.info("Evicting %s to free %d MB (VRAM budget)", entry.name, entry.vram_mb)
            self._unload_locked(entry.name)
            if used() + needed_mb <= budget:
                return

        raise ModelLoadError(
            f"Cannot free enough VRAM for {needed_mb} MB: "
            f"{used()} MB is held by models currently in use "
            f"(budget {budget} MB).")

    def _unload_locked(self, name: str) -> bool:
        """Unload one model. Caller must hold the lock."""
        entry = self._loaded.get(name)
        if entry is None:
            return False
        if entry.refcount > 0:
            return False

        spec = registry.get_model_spec(name)
        if spec is not None and spec.unloader:
            try:
                spec.unloader(entry.handle)
            except Exception:
                logger.exception("Custom unloader for %s failed", name)

        del self._loaded[name]
        entry.handle = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        logger.info("Unloaded model %s", name)
        return True

    def unload(self, name: str) -> bool:
        with self._lock:
            return self._unload_locked(name)

    def unload_all(self, force: bool = False) -> int:
        with self._lock:
            n = 0
            for name in list(self._loaded):
                if force:
                    self._loaded[name].refcount = 0
                if self._unload_locked(name):
                    n += 1
            return n

    # -- reaper -----------------------------------------------------------

    def reap_idle(self) -> list[str]:
        """Unload every unreferenced model idle past the timeout. Returns names."""
        now = time.monotonic()
        reaped = []
        with self._lock:
            for name, entry in list(self._loaded.items()):
                if entry.refcount == 0 and (now - entry.last_used) >= self.idle_timeout:
                    if self._unload_locked(name):
                        reaped.append(name)
        return reaped

    def start_reaper(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return

        def _loop():
            while not self._stop.wait(self.reaper_interval):
                try:
                    self.reap_idle()
                except Exception:
                    logger.exception("Model reaper sweep failed")

        self._stop.clear()
        self._reaper = threading.Thread(target=_loop, name="model-reaper", daemon=True)
        self._reaper.start()

    def stop_reaper(self) -> None:
        self._stop.set()
        if self._reaper:
            self._reaper.join(timeout=2)

    # -- introspection ----------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                "device": self.device,
                "vram_budget_mb": self.vram_budget_mb,
                "vram_used_mb": sum(e.vram_mb for e in self._loaded.values()),
                "idle_timeout_seconds": self.idle_timeout,
                "loaded": [
                    {"name": e.name, "vram_mb": e.vram_mb, "refcount": e.refcount,
                     "idle_seconds": round(now - e.last_used, 1),
                     "load_count": self._load_counts.get(e.name, 0)}
                    for e in self._loaded.values()
                ],
            }

    def load_count(self, name: str) -> int:
        return self._load_counts.get(name, 0)


_manager: Optional[ModelManager] = None
_manager_lock = threading.Lock()


def get_manager() -> ModelManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ModelManager()
        return _manager
