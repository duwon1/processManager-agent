"""Shared update policy helpers for agent lifecycle code."""
from __future__ import annotations

import re

COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def normalize_target_sha(value: object) -> str:
    """Return a lowercase Git SHA prefix/full SHA, or an empty string when invalid."""
    target_sha = str(value or "").strip()
    if not COMMIT_SHA_PATTERN.fullmatch(target_sha):
        return ""
    return target_sha.lower()
