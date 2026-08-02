"""Auto-sync sandbox allowed imports from pyproject.toml.

Scans [project].dependencies at startup, maps package names to importable
top-level module names, and produces the sandbox allowlist + LLM prompt string.
`uv add xyz` + restart is enough to make a new library available to sandboxed
code — no manual allowlist edit needed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# Map pip package names → importable module names (only when they differ)
_PACKAGE_TO_MODULE: dict[str, str] = {
    "opencv-python-headless": "cv2",
    "opencv-python": "cv2",
    "scikit-image": "skimage",
    "pillow": "PIL",
    "pydensecrf2": "pydensecrf",
}

# Always-allowed stdlib modules (safe for sandbox)
_STDLIB_ALWAYS: frozenset[str] = frozenset({
    "os", "pathlib", "math", "json", "re", "struct", "io",
    "typing", "dataclasses", "functools", "itertools",
    "collections", "copy", "warnings", "traceback",
    "time", "datetime", "enum", "abc", "contextlib",
})

# Packages that should NEVER be sandbox-importable (infrastructure deps)
_EXCLUDED_PACKAGES: frozenset[str] = frozenset({
    "flask", "gunicorn", "waitress", "python-dotenv",
    "pydantic", "requests", "google-genai", "openai", "authlib",
})


def _parse_package_name(dep_spec: str) -> str:
    """Extract bare package name from a PEP 508 dependency string."""
    return re.split(r"[><=!~\[;@\s]", dep_spec)[0].strip().lower()


def sync_from_pyproject(pyproject_path: Path | None = None) -> tuple[frozenset[str], str]:
    """Return (allowed_imports_set, library_whitelist_string) from pyproject.toml."""
    if pyproject_path is None:
        pyproject_path = Path(__file__).parent.parent.parent.parent.parent / "pyproject.toml"

    if not pyproject_path.exists():
        fallback = _STDLIB_ALWAYS | frozenset({"numpy", "np", "PIL", "cv2",
                                                "scipy", "skimage", "tifffile"})
        return fallback, "numpy, PIL/Pillow, cv2, scipy, skimage, tifffile"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    deps = data.get("project", {}).get("dependencies", [])
    sandbox_modules: set[str] = set()
    prompt_libs: list[str] = []

    for dep_spec in deps:
        pkg = _parse_package_name(dep_spec)
        if pkg in _EXCLUDED_PACKAGES:
            continue
        module = _PACKAGE_TO_MODULE.get(pkg, pkg.replace("-", "_"))
        sandbox_modules.add(module)
        prompt_libs.append(f"{module} ({pkg})" if module != pkg else module)

    if "numpy" in sandbox_modules:
        sandbox_modules.add("np")

    allowed = _STDLIB_ALWAYS | sandbox_modules
    stdlib_display = sorted(_STDLIB_ALWAYS & {"os", "pathlib", "math", "json", "re", "io", "typing"})
    prompt_str = (
        ", ".join(sorted(prompt_libs)) + ". "
        f"Standard library: {', '.join(stdlib_display)}. "
        "NO matplotlib, NO subprocess, NO socket, NO requests, NO threading."
    )

    return frozenset(allowed), prompt_str
