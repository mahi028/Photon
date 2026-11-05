"""Application configuration loaded from environment variables.

LLM_PROVIDER controls which LLM backend is used:
  gemini     — Google Gemini via google-genai SDK (default)
  openai     — OpenAI or any OpenAI-compatible API (set OPENAI_BASE_URL for custom endpoints,
               e.g. Ollama, Together, Groq, Mistral, etc.)
  openrouter — OpenRouter (OpenAI-compatible; model runtime-selectable from a
               dropdown backed by GET https://openrouter.ai/api/v1/models)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

from .core.sandbox.dep_sync import sync_from_pyproject

# Load .env from project root
_root = Path(__file__).parent.parent.parent
load_dotenv(_root / ".env")

# Auto-synced from pyproject.toml [project].dependencies at import time —
# `uv add xyz` + restart makes a new library sandbox-importable, no manual edit.
_SYNCED_SANDBOX_IMPORTS, _SYNCED_LIBRARY_WHITELIST = sync_from_pyproject(_root / "pyproject.toml")


class Config:
    # ---- LLM Provider selection ----
    # "gemini" (default) | "openai" (covers OpenAI + compatible APIs)
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini")

    # ---- Gemini (google-genai SDK) ----
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # ---- OpenAI / OpenAI-compatible ----
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # Leave empty for official OpenAI; set to custom URL for compatible APIs
    # e.g.: http://localhost:11434/v1 (Ollama)
    #       https://api.together.xyz/v1 (Together AI)
    #       https://api.groq.com/openai/v1 (Groq)
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")

    # ---- OpenRouter (OpenAI-compatible aggregator) ----
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    # Default model at startup; runtime-overridable via POST /api/llm/model
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    @property
    def active_model(self) -> str:
        """Resolved model name for the currently active LLM provider (startup default;
        the openrouter provider may be overridden at runtime — see llm client)."""
        provider = self.LLM_PROVIDER
        if provider == "openai":
            return self.OPENAI_MODEL
        if provider == "openrouter":
            return self.OPENROUTER_MODEL
        return self.GEMINI_MODEL

    # ---- LLM loop budget ----
    MAX_LOOP_ITERATIONS: int = 6
    EXECUTION_TIMEOUT_SECONDS: int = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "30"))

    # ---- Sandbox threading ----
    # Threads allowed for BLAS/OpenMP pools (OpenBLAS, MKL, numexpr, ...) inside
    # sandboxed code. Applied via *_NUM_THREADS env vars in the child process.
    # Keep at 1 on many-core machines: unrestricted OpenBLAS spawns a thread per
    # core, blowing through RLIMIT_CPU (counted across threads) and memory.
    # The sandbox CPU rlimit is scaled by this value so semantics stay sane.
    SANDBOX_BLAS_THREADS: int = int(os.getenv("SANDBOX_BLAS_THREADS", "1"))

    # ---- GPU model lifecycle (v3 P4 — see DECISIONS D-030) ----
    # Hard rule: a model loads only when an op actually asks for it, and is
    # unloaded once it has been idle this long. Enforced by ModelManager in the
    # sidecar, not by op authors remembering to free things.
    MODEL_IDLE_TIMEOUT_SECONDS: int = int(os.getenv("MODEL_IDLE_TIMEOUT_SECONDS", "300"))
    # Ceiling on total VRAM held by loaded models; LRU-evicts unreferenced
    # models to stay under it. 0 = derive from the device at runtime.
    MODEL_VRAM_BUDGET_MB: int = int(os.getenv("MODEL_VRAM_BUDGET_MB", "0"))
    # auto | cuda | cpu
    MODEL_DEVICE: str = os.getenv("MODEL_DEVICE", "auto")
    MODEL_SIDECAR_ENABLED: bool = os.getenv("MODEL_SIDECAR_ENABLED", "1") not in ("0", "false", "False")
    # How long the reaper sleeps between sweeps.
    MODEL_REAPER_INTERVAL_SECONDS: int = int(os.getenv("MODEL_REAPER_INTERVAL_SECONDS", "15"))
    # Sidecar exits after this long with zero models loaded (0 disables).
    MODEL_SIDECAR_IDLE_EXIT_SECONDS: int = int(os.getenv("MODEL_SIDECAR_IDLE_EXIT_SECONDS", "1800"))
    SANDBOX_MEMORY_LIMIT_GB: int = int(os.getenv("SANDBOX_MEMORY_LIMIT_GB", "8"))

    # ---- Session & Persistence ----
    MAX_HYDRATED_WINDOWS: int = 5

    # ---- Upload limits ----
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "200"))
    MAX_UPLOAD_BYTES: int = MAX_UPLOAD_MB * 1024 * 1024

    # Allowed upload extensions
    ALLOWED_EXTENSIONS: frozenset = frozenset(
        {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".npy", ".bmp", ".webp"}
    )

    # ---- Flask ----
    SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    ENV: str = os.getenv("FLASK_ENV", "development")
    DEBUG: bool = ENV == "development"

    # ---- Auth ----
    # Username+email+password is always available. Google OAuth is optional —
    # auto-enabled only when both env vars are set; otherwise the Google button
    # is hidden and its routes 404 (no crash, no forced setup).
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_OAUTH_ENABLED: bool = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

    # ---- MCP server ----
    # Externally reachable MCP endpoint (e.g. behind a port-forward/proxy), shown
    # on the API keys page. Empty = derived in the browser as <page host>:8890/mcp.
    MCP_PUBLIC_URL: str = os.getenv("PHOTON_MCP_PUBLIC_URL", "")

    # ---- Volume paths (relative to project root) ----
    ROOT_DIR: Path = _root
    VOLUMES_DIR: Path = _root / "volumes"
    UPLOADS_DIR: Path = VOLUMES_DIR / "uploads"
    OUTPUTS_DIR: Path = VOLUMES_DIR / "outputs"
    TMP_EXEC_DIR: Path = VOLUMES_DIR / "tmp_exec"
    # Model sidecar IPC: Unix socket + the scratch dir used to hand arrays
    # across the process boundary as .npy files (never through the socket).
    MODEL_SIDECAR_SOCKET: Path = Path(
        os.getenv("MODEL_SIDECAR_SOCKET", str(VOLUMES_DIR / "model_sidecar.sock")))
    MODEL_IPC_DIR: Path = VOLUMES_DIR / "model_ipc"

    # Preview settings
    PREVIEW_MAX_LONG_EDGE: int = 1024

    # Whitelisted imports for sandbox AST check — auto-synced from pyproject.toml
    # (dep_sync.py). Add a library via `uv add xyz` + restart, not by editing this.
    SANDBOX_ALLOWED_IMPORTS: frozenset = _SYNCED_SANDBOX_IMPORTS
    SANDBOX_LIBRARY_WHITELIST: str = _SYNCED_LIBRARY_WHITELIST

    # Sandbox executor class
    # Use DockerExecutor in production for security, SubprocessExecutor in development for speed
    # EXECUTOR_CLASS: str = "docker" if ENV == "production" else "subprocess"
    EXECUTOR_CLASS: str = "subprocess"


config = Config()

# Ensure volume directories exist at import time
for _d in (config.UPLOADS_DIR, config.OUTPUTS_DIR, config.TMP_EXEC_DIR, config.MODEL_IPC_DIR):
    _d.mkdir(parents=True, exist_ok=True)
