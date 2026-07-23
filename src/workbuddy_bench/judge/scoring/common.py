"""Shared ScoreResult post-processing helpers."""

from __future__ import annotations

from workbuddy_bench.judge.core import ScoreResult


def first_judge_duration(score: ScoreResult) -> float | None:
    for result in score.judge_results:
        value = result.metadata.get("duration_sec")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def add_standard_numeric_fields(score: ScoreResult) -> None:
    """Populate Harbor-facing numeric fields common to native verifier profiles."""

    score.numeric.setdefault("test_pass_rate", score.reward)
    score.numeric.setdefault("overall", score.reward)
    duration = first_judge_duration(score)
    if duration is not None:
        score.numeric.setdefault("wall_time_sec", round(duration, 2))
        score.metadata.setdefault("wall_time_sec", round(duration, 2))
