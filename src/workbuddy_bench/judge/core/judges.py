"""Base judge runner classes for CompositeVerifier."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable

from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvaluationContext,
    EvaluationPlan,
    JudgeFamily,
    JudgeResult,
    JudgeSpec,
)


class BaseJudgeRunner(ABC):
    """Base class for judge runners executed by :class:`CompositeVerifierEngine`."""

    judge_family: JudgeFamily = JudgeFamily.RULE

    @abstractmethod
    def run(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
    ) -> JudgeResult | Awaitable[JudgeResult]:
        """Return normalized verdicts for one configured judge."""


class RuleJudgeRunner(BaseJudgeRunner):
    """Deterministic or programmatic judge runner."""

    judge_family = JudgeFamily.RULE


class LLMJudgeRunner(BaseJudgeRunner):
    """Model-based judge runner over collected evidence and rubric items."""

    judge_family = JudgeFamily.LLM


class VLMJudgeRunner(BaseJudgeRunner):
    """Vision-capable model judge runner over visual evidence and rubrics."""

    judge_family = JudgeFamily.VLM


class AgentJudgeRunner(BaseJudgeRunner):
    """Interactive agent-based judge runner over the target workspace."""

    judge_family = JudgeFamily.AGENT
