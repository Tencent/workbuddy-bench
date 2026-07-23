"""Minimal scoring policies for the verifier engine skeleton."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvaluationContext,
    EvaluationPlan,
    JudgeResult,
    JudgeVerdict,
    ScoreResult,
    VerdictStatus,
)

_SCORABLE_STATUSES = {
    VerdictStatus.PASS,
    VerdictStatus.FAIL,
    VerdictStatus.BUILD_ERROR,
    VerdictStatus.JUDGE_ERROR,
}

_FAILURE_STATUSES = {
    VerdictStatus.FAIL,
    VerdictStatus.BUILD_ERROR,
    VerdictStatus.JUDGE_ERROR,
}


def _status_counts(verdicts: Sequence[JudgeVerdict]) -> dict[str, int]:
    counts = {status.value: 0 for status in VerdictStatus}
    for verdict in verdicts:
        counts[verdict.status.value] += 1
    return counts


def _group_scorable_by_item(
    verdicts: Sequence[JudgeVerdict],
) -> dict[str, list[JudgeVerdict]]:
    verdicts_by_item: dict[str, list[JudgeVerdict]] = {}
    for verdict in verdicts:
        if verdict.status not in _SCORABLE_STATUSES:
            continue
        verdicts_by_item.setdefault(verdict.item_id, []).append(verdict)
    return verdicts_by_item


def _mean_score(verdicts: Sequence[JudgeVerdict]) -> float:
    if not verdicts:
        return 0.0
    return sum(verdict.score or 0.0 for verdict in verdicts) / len(verdicts)


def _item_score(verdicts: Sequence[JudgeVerdict]) -> float:
    """Aggregate multiple judges' scores for a single item conservatively.

    When several judges score the same item, the worst verdict wins (min), so a
    FAIL from one judge cannot be diluted to partial credit by a PASS from
    another. A single verdict (the common case, incl. a fractional aggregate
    score) passes through unchanged.
    """
    if not verdicts:
        return 0.0
    return min(verdict.score or 0.0 for verdict in verdicts)


def _test_status(reward: float, *, fatal_status: VerdictStatus | None = None) -> str:
    if fatal_status is not None:
        return fatal_status.value
    if reward >= 1.0:
        return "full_pass"
    if reward > 0.0:
        return "partial_pass"
    return "no_pass"


def _coerce_cap(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


def _metadata_enabled(metadata: Mapping[str, Any], keys: Sequence[str]) -> bool:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "required"}
        return bool(value)
    return False


def _metadata_cap(metadata: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata[key]
        if isinstance(value, Mapping):
            value = value.get("max_score", value.get("cap"))
        if value is None or value is False:
            continue
        return _coerce_cap(f"metadata[{key!r}]", value)
    return None


def _coerce_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        number = int(value)
        return number if number >= 0 else None
    return None


def _real_test_counts(
    judge_results: Sequence[JudgeResult],
) -> tuple[int | None, int | None]:
    """Sum real ``tests_passed`` / ``tests_total`` from rule-runner metadata.

    Rule runners carry the deterministic pytest/script counts in
    ``JudgeResult.metadata``. The pass-rate policy otherwise only knows about
    plan items (often a single aggregate item), so surfacing these keeps the
    diagnostic counts faithful to the real test run. Returns ``(None, None)``
    when no runner reported counts so callers can fall back to item counts.
    """
    passed_total = 0
    total_total = 0
    seen = False
    for result in judge_results:
        metadata = result.metadata or {}
        total = _coerce_count(metadata.get("tests_total"))
        if total is None:
            continue
        passed = _coerce_count(metadata.get("tests_passed")) or 0
        if passed > total:
            passed = total
        passed_total += passed
        total_total += total
        seen = True
    if not seen:
        return None, None
    return passed_total, total_total


def _is_partial_aggregate_verdict(verdict: JudgeVerdict) -> bool:
    score = verdict.score
    return bool(
        verdict.metadata.get("partial_aggregate_score")
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and 0.0 < float(score) < 1.0
    )


def _cap_on_partial(verdict: JudgeVerdict, item_metadata: Mapping[str, Any] | None) -> bool:
    # Aggregate-only reward payloads encode partial credit as a single FAIL
    # verdict with score in (0, 1). That FAIL status should continue to count in
    # pass-rate scoring, but it is not a hard failure by itself. Profiles that
    # truly want a partial aggregate score to trigger a hard-fail cap must opt in
    # explicitly with cap_on_partial on the verdict or item metadata.
    return _metadata_enabled(
        verdict.metadata,
        ("cap_on_partial",),
    ) or _metadata_enabled(item_metadata or {}, ("cap_on_partial",))


@dataclass
class PassRateScoringPolicy:
    """Simple item pass-rate policy for deterministic rule judges.

    This is not intended to be the final Web scoring policy. It provides the
    smallest useful shared behavior for synthetic tests and for deterministic
    code/office policies:

    - pass/fail/build_error/judge_error verdicts count in the denominator;
    - review/abstain verdicts stay in diagnostics but do not affect the score;
    - item weights come from ``EvaluationPlan.items``;
    - judge errors can either be represented as failed items or force a fatal
      zero score, depending on ``fatal_judge_error``.

    Phase 3 also supports lightweight gate/cap semantics through item or verdict
    metadata. Profiles can mark an item with ``{"pass_gate": true}`` or
    ``{"required": true}`` to force score 0 unless that item has a pass verdict,
    and ``{"hard_fail_cap": 0.2}`` / ``{"score_cap": 0.2}`` to cap the final
    score when that item receives a fail/build_error/judge_error verdict. Web's
    richer weighted checklist policy can later reuse the same idea with a more
    explicit profile schema.
    """

    fatal_judge_error: bool = True
    gate_metadata_keys: tuple[str, ...] = ("pass_gate", "required")
    cap_metadata_keys: tuple[str, ...] = (
        "hard_fail_cap",
        "score_cap_on_fail",
        "score_cap",
    )
    metadata: dict[str, object] = field(default_factory=dict)

    def score(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge_results: Sequence[JudgeResult],
    ) -> ScoreResult:
        verdicts = [verdict for result in judge_results for verdict in result.verdicts]
        items_by_id = {item.id: item for item in plan.items}
        item_weights = {item.id: item.weight for item in plan.items}
        scorable = [verdict for verdict in verdicts if verdict.status in _SCORABLE_STATUSES]

        fatal_status: VerdictStatus | None = None
        if any(result.status == VerdictStatus.BUILD_ERROR for result in judge_results):
            fatal_status = VerdictStatus.BUILD_ERROR
        elif self.fatal_judge_error and any(
            result.status == VerdictStatus.JUDGE_ERROR for result in judge_results
        ):
            fatal_status = VerdictStatus.JUDGE_ERROR

        if fatal_status is not None:
            reward = 0.0
        else:
            scorable_by_item = _group_scorable_by_item(scorable)
            # Denominator spans EVERY declared plan item, not just the ones a
            # judge covered. An item with no scorable verdict is an evaluation
            # gap and scores 0 — otherwise uncovered checks silently vanish from
            # the denominator and an all-covered subset reads as a full pass.
            if plan.items:
                scored_item_ids = [item.id for item in plan.items]
            else:
                # Synthetic plans with no declared items: fall back to whatever
                # items produced verdicts so scoring still works.
                scored_item_ids = list(scorable_by_item)
            total_weight = sum(
                item_weights.get(item_id, 1.0)
                for item_id in scored_item_ids
            )
            if total_weight <= 0:
                reward = 0.0
            else:
                reward = sum(
                    _item_score(scorable_by_item.get(item_id, ()))
                    * item_weights.get(item_id, 1.0)
                    for item_id in scored_item_ids
                ) / total_weight

        raw_reward = reward
        scorable_by_item = _group_scorable_by_item(scorable)
        duplicate_scorable_item_ids = sorted(
            item_id
            for item_id, item_verdicts in scorable_by_item.items()
            if len(item_verdicts) > 1
        )
        failed_gates: list[dict[str, Any]] = []
        applied_caps: list[dict[str, Any]] = []

        verdicts_by_item: dict[str, list[JudgeVerdict]] = {}
        for verdict in verdicts:
            verdicts_by_item.setdefault(verdict.item_id, []).append(verdict)

        for item in plan.items:
            if not _metadata_enabled(item.metadata, self.gate_metadata_keys):
                continue
            item_verdicts = verdicts_by_item.get(item.id, [])
            if item_verdicts and any(
                verdict.status == VerdictStatus.PASS for verdict in item_verdicts
            ):
                continue
            failed_gates.append(
                {
                    "item_id": item.id,
                    "reason": "required item did not receive a pass verdict",
                    "statuses": [verdict.status.value for verdict in item_verdicts],
                }
            )

        for verdict in verdicts:
            if verdict.status not in _FAILURE_STATUSES:
                continue
            item = items_by_id.get(verdict.item_id)
            if _is_partial_aggregate_verdict(verdict) and not _cap_on_partial(
                verdict,
                item.metadata if item is not None else None,
            ):
                continue
            cap = _metadata_cap(verdict.metadata, self.cap_metadata_keys)
            cap_source = "verdict"
            if cap is None and item is not None:
                cap = _metadata_cap(item.metadata, self.cap_metadata_keys)
                cap_source = "item"
            if cap is None:
                continue
            applied_caps.append(
                {
                    "item_id": verdict.item_id,
                    "status": verdict.status.value,
                    "cap": cap,
                    "source": cap_source,
                }
            )

        if failed_gates:
            reward = 0.0
        elif applied_caps:
            reward = min(reward, *(entry["cap"] for entry in applied_caps))

        tests_passed, tests_total = _real_test_counts(judge_results)
        if tests_total is None:
            # No runner reported real test counts (e.g. LLM-only or synthetic
            # plans). Fall back to per-item pass counts so the diagnostic fields
            # stay populated. Mirror the reward denominator: count every declared
            # plan item, treat an uncovered item as not-passed, and require all
            # judges to pass an item (min) before it counts.
            fallback_item_ids = (
                [item.id for item in plan.items] if plan.items else list(scorable_by_item)
            )
            tests_passed = sum(
                1
                for item_id in fallback_item_ids
                if _item_score(scorable_by_item.get(item_id, ())) >= 1.0
            )
            tests_total = len(fallback_item_ids)

        return ScoreResult(
            reward=round(reward, 4),
            test_status=_test_status(reward, fatal_status=fatal_status),
            tests_passed=tests_passed,
            tests_total=tests_total,
            judge_results=list(judge_results),
            diagnostics={
                "policy": {
                    "name": "pass_rate",
                    "fatal_judge_error": self.fatal_judge_error,
                    "gate_metadata_keys": list(self.gate_metadata_keys),
                    "cap_metadata_keys": list(self.cap_metadata_keys),
                    "metadata": self.metadata,
                },
                "status_counts": _status_counts(verdicts),
                "score_adjustments": {
                    "raw_reward": round(raw_reward, 4),
                    "duplicate_scorable_item_ids": duplicate_scorable_item_ids,
                    "failed_gates": failed_gates,
                    "applied_caps": applied_caps,
                    "final_reward": round(reward, 4),
                },
                "evidence": evidence.to_dict(),
                "plan": plan.to_dict(),
                "context": context.to_dict(),
            },
            metadata={"scoring_policy": "pass_rate"},
        )
