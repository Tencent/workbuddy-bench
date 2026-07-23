"""Normalize JSON reward payloads into rule judge verdicts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbuddy_bench.judge.core import EvaluationContext, JudgeSpec, JudgeVerdict, VerdictStatus


@dataclass(frozen=True)
class PayloadReadResult:
    payload: dict[str, Any] | None
    path: Path | None
    source: str = ""
    checked_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.payload is not None


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "pass", "passed"}
    return bool(value)


def as_unit_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def score_from_payload(payload: dict[str, Any]) -> float | None:
    for key in ("reward", "overall", "test_pass_rate", "score"):
        score = as_unit_float(payload.get(key))
        if score is not None:
            return score
    return None


def payload_has_counts(payload: dict[str, Any]) -> bool:
    if as_int(payload.get("tests_passed")) is not None or as_int(payload.get("tests_total")) is not None:
        return True
    tests = payload.get("tests")
    return isinstance(tests, list) and any(isinstance(entry, dict) for entry in tests)


def payload_is_scorable(payload: dict[str, Any]) -> bool:
    return payload_has_counts(payload) or score_from_payload(payload) is not None


def counts_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    passed = as_int(payload.get("tests_passed"))
    total = as_int(payload.get("tests_total"))
    tests = payload.get("tests")
    if (passed is None or total is None) and isinstance(tests, list):
        entries = [entry for entry in tests if isinstance(entry, dict)]
        passed = sum(1 for entry in entries if as_bool(entry.get("passed")))
        total = len(entries)
    passed = 0 if passed is None or passed < 0 else passed
    total = 0 if total is None or total < 0 else total
    if passed > total:
        passed = total
    if total == 0 and score_from_payload(payload) is not None:
        score = score_from_payload(payload) or 0.0
        return (1 if score >= 1.0 else 0, 1)
    return passed, total


def status_from_payload(payload: dict[str, Any], passed: int, total: int) -> str:
    status = payload.get("test_status")
    if isinstance(status, str) and status:
        return "full_pass" if status == "pass" else status
    score = score_from_payload(payload)
    if score is not None:
        if score >= 1.0:
            return "full_pass"
        if score > 0.0:
            return "partial_pass"
        return "no_pass"
    rate = passed / total if total > 0 else 0.0
    if total <= 0:
        return "build_error"
    if rate >= 1.0:
        return "full_pass"
    if rate > 0:
        return "partial_pass"
    return "no_pass"


def verdicts_from_payload(
    *,
    judge: JudgeSpec,
    payload: dict[str, Any],
    passed: int,
    total: int,
    item_ids: list[str] | None = None,
) -> list[JudgeVerdict]:
    # Generic/legacy scorers may emit both an aggregate score and per-check
    # diagnostics. The aggregate is the canonical score and must be attached to
    # the declared plan item IDs; otherwise named checks do not match the plan
    # and the scoring policy treats every declared item as uncovered.
    aggregate_score = score_from_payload(payload)
    if aggregate_score is not None:
        return aggregate_verdicts(
            judge=judge,
            item_ids=item_ids,
            score=aggregate_score,
            metadata=_aggregate_count_metadata(
                payload,
                score=aggregate_score,
                passed=passed,
                total=total,
            ),
        )

    tests = payload.get("tests")
    if isinstance(tests, list) and tests:
        verdicts: list[JudgeVerdict] = []
        seen: dict[str, int] = {}
        for entry in tests:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "check")
            n = seen.get(name, 0)
            seen[name] = n + 1
            item_id = name if n == 0 else f"{name}#{n}"
            verdicts.append(
                JudgeVerdict(
                    item_id=item_id,
                    status=VerdictStatus.PASS if as_bool(entry.get("passed")) else VerdictStatus.FAIL,
                    judge_name=judge.name,
                    judge_type=judge.type,
                    reason=str(entry.get("detail") or ""),
                    metadata={
                        key: value
                        for key, value in entry.items()
                        if key not in {"name", "passed", "detail"}
                    },
                )
            )
        if verdicts:
            return verdicts
    return synthetic_verdicts(judge=judge, passed=passed, total=total)


def _aggregate_count_metadata(
    payload: dict[str, Any],
    *,
    score: float,
    passed: int,
    total: int,
) -> dict[str, Any]:
    if not payload_has_counts(payload) or total <= 0:
        return {}
    count_score = max(0.0, min(passed / total, 1.0))
    if abs(count_score - score) <= 1e-6:
        return {"counts_score": round(count_score, 4)}
    return {
        "counts_score": round(count_score, 4),
        "aggregate_counts_mismatch": True,
    }


def aggregate_verdicts(
    *,
    judge: JudgeSpec,
    item_ids: list[str] | None,
    score: float,
    metadata: dict[str, Any] | None = None,
) -> list[JudgeVerdict]:
    targets = item_ids or judge.item_ids or [f"{judge.name}::overall"]
    # The aggregate score has no per-check binary verdicts. We keep the
    # historical PASS/FAIL status mapping for compatibility, but mark partial
    # aggregate scores so cap logic can tell "partial credit" apart from a true
    # hard failure.
    verdict_metadata = {"aggregate_score_verdict": True}
    verdict_metadata.update(metadata or {})
    if 0.0 < score < 1.0:
        verdict_metadata["partial_aggregate_score"] = True
    return [
        JudgeVerdict(
            item_id=item_id,
            status=VerdictStatus.PASS if score >= 1.0 else VerdictStatus.FAIL,
            judge_name=judge.name,
            judge_type=judge.type,
            score=score,
            metadata=dict(verdict_metadata),
        )
        for item_id in targets
    ]


def synthetic_verdicts(*, judge: JudgeSpec, passed: int, total: int) -> list[JudgeVerdict]:
    return [
        JudgeVerdict(
            item_id=f"{judge.name}::check::{index}",
            status=VerdictStatus.PASS if index < passed else VerdictStatus.FAIL,
            judge_name=judge.name,
            judge_type=judge.type,
        )
        for index in range(max(0, total))
    ]


def read_score_reward_payload(
    *,
    context: EvaluationContext,
    cwd: Path,
    config: dict[str, Any],
) -> PayloadReadResult:
    """Read a script's score/reward JSON, preferring rich ``score.json``."""

    candidates = payload_candidate_paths(context=context, cwd=cwd, config=config)
    checked: list[Path] = []
    errors: list[str] = []
    for source, path in candidates:
        checked.append(path)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            errors.append(f"{source} at {path} is not valid JSON: {exc}")
            continue
        except OSError as exc:
            errors.append(f"{source} at {path} could not be read: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{source} at {path} must contain a JSON object")
            continue
        if not payload_is_scorable(payload):
            errors.append(
                f"{source} at {path} must include overall, reward, "
                "test_pass_rate, tests_passed/tests_total, or tests[]"
            )
            continue
        return PayloadReadResult(
            payload=payload,
            path=path,
            source=source,
            checked_paths=checked,
            errors=errors,
        )
    return PayloadReadResult(
        payload=None,
        path=None,
        checked_paths=checked,
        errors=errors,
    )


def payload_candidate_paths(
    *,
    context: EvaluationContext,
    cwd: Path,
    config: dict[str, Any],
) -> list[tuple[str, Path]]:
    if config.get("result_json"):
        return [("result_json", resolve_payload_path(config["result_json"], context=context, cwd=cwd))]

    candidates: list[tuple[str, Path]] = []
    score_json = config.get("score_json", "score.json")
    reward_json = config.get("reward_json", "reward.json")
    candidates.append(("score_json", resolve_payload_path(score_json, context=context, cwd=cwd)))
    candidates.append(("reward_json", resolve_payload_path(reward_json, context=context, cwd=cwd)))
    return _dedupe_candidates(candidates)


def resolve_payload_path(raw: Any, *, context: EvaluationContext, cwd: Path) -> Path:
    path = Path(str(raw))
    host_verifier_dir = context.host_paths.get("verifier_dir")
    if path.is_absolute():
        if context.verifier_dir and host_verifier_dir:
            try:
                relative = path.relative_to(context.verifier_dir)
            except ValueError:
                return path
            return Path(host_verifier_dir) / relative
        return path
    if host_verifier_dir and path.name in {"score.json", "reward.json"} and len(path.parts) == 1:
        return Path(host_verifier_dir) / path
    return cwd / path


def _dedupe_candidates(candidates: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    seen: set[Path] = set()
    deduped: list[tuple[str, Path]] = []
    for source, path in candidates:
        if path in seen:
            continue
        seen.add(path)
        deduped.append((source, path))
    return deduped
