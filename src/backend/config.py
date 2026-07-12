"""Application configuration loaded from environment variables.

LLM_PROVIDER controls which LLM backend is used:
  gemini    — Google Gemini via google-genai SDK (default)
  openai    — OpenAI or any OpenAI-compatible API (set OPENAI_BASE_URL for custom endpoints,
              e.g. Ollama, Together, Groq, Mistral, etc.)
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_root = Path(__file__).parent.parent.parent
load_dotenv(_root / ".env")


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

    @property
    def active_model(self) -> str:
        """Resolved model name for the currently active LLM provider."""
        return self.OPENAI_MODEL if self.LLM_PROVIDER == "openai" else self.GEMINI_MODEL

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

    # ---- Volume paths (relative to project root) ----
    ROOT_DIR: Path = _root
    VOLUMES_DIR: Path = _root / "volumes"
    UPLOADS_DIR: Path = VOLUMES_DIR / "uploads"
    OUTPUTS_DIR: Path = VOLUMES_DIR / "outputs"
    TMP_EXEC_DIR: Path = VOLUMES_DIR / "tmp_exec"

    # Preview settings
    PREVIEW_MAX_LONG_EDGE: int = 1024

    # Whitelisted imports for sandbox AST check
    SANDBOX_ALLOWED_IMPORTS: frozenset = frozenset(
        {"numpy", "np", "PIL", "Image", "cv2", "scipy", "skimage", "tifffile", "os", "pathlib"}
    )

    # Sandbox executor class
    # Use DockerExecutor in production for security, SubprocessExecutor in development for speed
    # EXECUTOR_CLASS: str = "docker" if ENV == "production" else "subprocess"
    EXECUTOR_CLASS: str = "subprocess"


config = Config()

# Ensure volume directories exist at import time
for _d in (config.UPLOADS_DIR, config.OUTPUTS_DIR, config.TMP_EXEC_DIR):
    _d.mkdir(parents=True, exist_ok=True)
