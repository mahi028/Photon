"""Where untrusted code runs: the one switch callers go through (D-038).

  PHOTON_SANDBOX=local   (default) subprocess on this host: AST check,
                         rlimits, runtime guard (guard.py). Fine for a
                         trusted team; not a hard boundary.
  PHOTON_SANDBOX=remote  ship jobs to a sandbox worker (src/sandbox_worker)
                         running under gVisor, optionally with a GPU. Needs
                         PHOTON_SANDBOX_URL + PHOTON_SANDBOX_TOKEN.

Both backends accept the same inputs and return the same result shapes, so
routes and the MCP server never know which one ran the job.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .base import Executor

MODES = ("local", "remote")


def mode() -> str:
    m = os.getenv("PHOTON_SANDBOX", "local").strip().lower()
    if m not in MODES:
        raise ValueError(f"PHOTON_SANDBOX must be one of {MODES}, got {m!r}")
    return m


def get_executor() -> Executor:
    """Executor for run-code jobs (manual editor, LLM code loop)."""
    if mode() == "remote":
        from .remote import RemoteExecutor
        return RemoteExecutor()
    from .subprocess_executor import SubprocessExecutor
    return SubprocessExecutor()


def run_pipeline(graph: dict, input_path: str, output_dir: str, node_preview_dir: str,
                 custom_nodes: dict | None = None, timeout: Optional[int] = None,
                 input_mode: str = "RGB", assets: dict[str, str] | None = None,
                 export_tables: bool = False) -> dict[str, Any]:
    """Run a pipeline graph against one image (see executor.run_pipeline_subprocess)."""
    if mode() == "remote":
        from .remote import run_pipeline_remote
        return run_pipeline_remote(graph, input_path, output_dir, node_preview_dir,
                                   custom_nodes=custom_nodes, timeout=timeout, input_mode=input_mode,
                                   assets=assets, export_tables=export_tables)
    from ..pipeline.executor import run_pipeline_subprocess
    return run_pipeline_subprocess(graph, input_path, output_dir, node_preview_dir,
                                   custom_nodes=custom_nodes, timeout=timeout, input_mode=input_mode,
                                   assets=assets, export_tables=export_tables)
