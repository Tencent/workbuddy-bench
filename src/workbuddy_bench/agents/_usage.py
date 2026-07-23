from __future__ import annotations

from typing import Any


def cached_input_tokens(usage: dict[str, Any]) -> int:
    """Return all cached input tokens reported by a stream-json usage object."""
    return int(usage.get("cache_read_input_tokens") or 0) + int(
        usage.get("cache_creation_input_tokens") or 0
    )
