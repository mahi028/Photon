"""File validation helpers for uploads."""

from pathlib import Path
from ..config import config


def validate_extension(filename: str) -> tuple[bool, str]:
    """Check whether a filename has an allowed extension.

    Returns:
        (ok, error_message) — error_message is empty string if ok.
    """
    ext = Path(filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        return False, (
            f"Extension '{ext}' not allowed. "
            f"Allowed: {', '.join(sorted(config.ALLOWED_EXTENSIONS))}"
        )
    return True, ""


def validate_size(size_bytes: int) -> tuple[bool, str]:
    """Check whether a file size is within the configured limit.

    Returns:
        (ok, error_message)
    """
    if size_bytes > config.MAX_UPLOAD_BYTES:
        return False, (
            f"File too large: {size_bytes / 1024 / 1024:.1f} MB. "
            f"Max allowed: {config.MAX_UPLOAD_MB} MB."
        )
    return True, ""
