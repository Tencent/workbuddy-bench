"""Pure-Python verifier orchestration engine.

This module is Phase 2 of the CompositeVerifier refactor. It intentionally has
no Harbor imports and does not know about code/office/web task layouts. The
Harbor-facing ``CompositeVerifier`` can later become a thin adapter that builds
an :class:`EvaluationContext`, resolves a profile-specific plan, and delegates
to this engine.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Mapping, Protocol, Sequence

from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvaluationContext,
    EvaluationPlan,
    JudgeFamily,
    JudgeResult,
    JudgeSpec,
    JudgeVerdict,
    ScoreResult,
    VerdictStatus,
)


class EvidenceCollector(Protocol):
    """Collect upstream evidence before any judge runs."""

    def collect(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
    ) -> EvidenceBundle | Awaitable[EvidenceBundle]:
        """Return evidence for this attempt."""


class JudgeRunner(Protocol):
    """Run one configured judge against collected evidence."""

    def run(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
    ) -> JudgeResult | Awaitable[JudgeResult]:
        """Return normalized verdicts for ``judge``."""


class ScoringPolicy(Protocol):
    """Resolve judge verdicts and aggregate them into a final score."""

    def score(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge_results: Sequence[JudgeResult],
    ) -> ScoreResult:
        """Return final verifier output before artifact writing."""


def _runner_family(runner: JudgeRunner) -> JudgeFamily | None:
    raw = getattr(runner, "judge_family", None)
    if raw is None:
        return None
    return JudgeFamily(raw)


def _result_with_family(result: JudgeResult, family: JudgeFamily) -> JudgeResult:
    if result.judge_family is None:
        result.judge_family = family
    result.metadata.setdefault("judge_family", str(result.judge_family))
    return result


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def _merge_evidence(bundles: Sequence[EvidenceBundle]) -> EvidenceBundle:
    merged = EvidenceBundle()
    for bundle in bundles:
        for record in bundle.records:
            merged.add(record)
        merged.metadata.update(bundle.metadata)
    return merged


def _target_item_ids(plan: EvaluationPlan, judge: JudgeSpec) -> list[str]:
    if judge.item_ids:
        return list(judge.item_ids)
    return [item.id for item in plan.items]


def _judge_error_result(
    judge: JudgeSpec,
    message: str,
    *,
    plan: EvaluationPlan,
    exception: BaseException | None = None,
) -> JudgeResult:
    """Convert runner lookup/execution failures into normalized judge output."""
    verdicts = [
        JudgeVerdict(
            item_id=item_id,
            status=VerdictStatus.JUDGE_ERROR,
            judge_name=judge.name,
            judge_type=judge.type,
            reason=message,
            metadata={"exception_type": type(exception).__name__} if exception else {},
        )
        for item_id in _target_item_ids(plan, judge)
    ]
    return JudgeResult(
        judge_name=judge.name,
        judge_type=judge.type,
        judge_family=judge.family,
        status=VerdictStatus.JUDGE_ERROR,
        verdicts=verdicts,
        errors=[message],
        metadata={"exception_type": type(exception).__name__} if exception else {},
    )


@dataclass
class CompositeVerifierEngine:
    """Dataset-neutral orchestration for one evaluation attempt.

    The engine owns lifecycle ordering only:

    ``collect evidence -> run judges -> score``.

    Dataset behavior belongs in evidence collectors, judge runners, and scoring
    policies. This split is what lets later phases support office/code/web
    without turning the Harbor-facing verifier into a large conditional.
    """

    evidence_collectors: Sequence[EvidenceCollector] = field(default_factory=list)
    judge_runners: Mapping[str, JudgeRunner] = field(default_factory=dict)
    scoring_policy: ScoringPolicy | None = None

    async def run(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
    ) -> ScoreResult:
        if self.scoring_policy is None:
            raise ValueError("CompositeVerifierEngine requires a scoring_policy")

        try:
            evidence = await self._collect_evidence(context, plan)
        except Exception as exc:
            return ScoreResult(
                reward=0.0,
                test_status=VerdictStatus.BUILD_ERROR.value,
                tests_passed=0,
                tests_total=0,
                diagnostics={
                    "engine_error": "evidence collection failed",
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                    "context": context.to_dict(),
                    "plan": plan.to_dict(),
                },
            )

        judge_results: list[JudgeResult] = []
        for judge in plan.judges:
            runner = self.judge_runners.get(judge.type)
            if runner is None:
                judge_results.append(
                    _judge_error_result(
                        judge,
                        f"no runner registered for judge type {judge.type!r}",
                        plan=plan,
                    )
                )
                continue
            try:
                runner_family = _runner_family(runner)
            except ValueError as exc:
                judge_results.append(
                    _judge_error_result(
                        judge,
                        f"runner for judge type {judge.type!r} has invalid family",
                        plan=plan,
                        exception=exc,
                    )
                )
                continue
            if runner_family is not None and runner_family != judge.family:
                judge_results.append(
                    _judge_error_result(
                        judge,
                        (
                            f"runner for judge type {judge.type!r} is family "
                            f"{runner_family.value!r}, but plan declares "
                            f"{judge.family.value!r}"
                        ),
                        plan=plan,
                    )
                )
                continue
            try:
                result = await _maybe_await(runner.run(context, plan, evidence, judge))
            except Exception as exc:
                judge_results.append(
                    _judge_error_result(
                        judge,
                        str(exc) or f"{type(exc).__name__} raised by judge runner",
                        plan=plan,
                        exception=exc,
                    )
                )
                continue
            if runner_family is not None:
                result = _result_with_family(result, runner_family)
            judge_results.append(result)

        return self.scoring_policy.score(context, plan, evidence, judge_results)

    async def _collect_evidence(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
    ) -> EvidenceBundle:
        bundles = [
            await _maybe_await(collector.collect(context, plan))
            for collector in self.evidence_collectors
        ]
        return _merge_evidence(bundles)
