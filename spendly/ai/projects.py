"""Projects tagger and handler - Phase 16.

Allows hashtags in expenses to be extracted and used for goals/projects.
"""

from __future__ import annotations

import re


def extract_tags(text: str) -> list[str]:
    """Extract standard hashtags from text."""
    if not text:
        return []
    tags = re.findall(r"#(\w+)", text)
    return [t.lower() for t in tags]


def strip_tags(text: str) -> str:
    """Remove hashtags from text to feed clean context."""
    if not text:
        return ""
    return re.sub(r"#\w+", "", text).strip()
