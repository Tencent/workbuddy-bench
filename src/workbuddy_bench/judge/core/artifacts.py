"""Artifact writing helpers for verifier outputs."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workbuddy_bench.judge.core.models import ScoreResult


def validate_numeric_payload(payload: dict[str, Any]) -> dict[str, float | int]:
    """Return ``payload`` if it is valid Harbor-facing numeric reward data."""
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(
                f"reward payload field {key!r} must be numeric, "
                f"got {type(value).__name__}"
            )
        if not math.isfinite(float(value)):
            raise ValueError(f"reward payload field {key!r} must be finite")
    return payload


@dataclass
class ArtifactWriter:
    """Write the canonical reward/score artifact pair."""

    reward_json_path: Path
    score_json_path: Path

    def write(self, result: ScoreResult) -> None:
        reward = validate_numeric_payload(result.reward_payload())
        score = result.score_payload()

        self.reward_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.score_json_path.parent.mkdir(parents=True, exist_ok=True)
        _replace_json(self.reward_json_path, reward, ensure_ascii=True)
        _replace_json(self.score_json_path, score, ensure_ascii=False)


def _replace_json(path: Path, payload: dict[str, Any], *, ensure_ascii: bool) -> None:
    """Write JSON by replacing the target path from the same directory.

    Container-backed rule runners may leave root- or sandbox-owned files in the
    host verifier directory. Replacing the directory entry avoids reopening that
    existing file for truncation while keeping the final artifact path stable.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=ensure_ascii)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
