"""Filename slugification utility."""

import re


def slugify(text: str, max_length: int = 50) -> str:
    """Convert arbitrary text to a safe filename segment.

    Args:
        text: Input string.
        max_length: Maximum characters in output.

    Returns:
        Lowercase, hyphen-separated, filesystem-safe string.
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")
    return text[:max_length]
