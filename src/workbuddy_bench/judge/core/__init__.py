"""Core evaluation contracts for the CompositeVerifier refactor.

This package is intentionally free of Harbor and dataset-specific imports. It
defines the common value objects used by later phases:

    Evidence -> Judges -> ScoringPolicy -> Artifacts
"""

from __future__ import annotations

from workbuddy_bench.judge.core.artifacts import ArtifactWriter, validate_numeric_payload
from workbuddy_bench.judge.core.engine import (
    CompositeVerifierEngine,
    EvidenceCollector,
    JudgeRunner,
    ScoringPolicy,
)
from workbuddy_bench.judge.core.judges import (
    AgentJudgeRunner,
    BaseJudgeRunner,
    LLMJudgeRunner,
    RuleJudgeRunner,
    VLMJudgeRunner,
)
from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvidenceRecord,
    EvaluationContext,
    EvaluationItem,
    EvaluationPlan,
    JudgeFamily,
    JudgeResult,
    JudgeSpec,
    JudgeVerdict,
    ScoreResult,
    VerdictStatus,
)
from workbuddy_bench.judge.core.routing import (
    VERIFIER_LLM_ENV_PREFIX,
    VerifierLLMRoute,
    verifier_llm_route_from_mapping,
)
from workbuddy_bench.judge.core.scoring import PassRateScoringPolicy

__all__ = [
    "ArtifactWriter",
    "AgentJudgeRunner",
    "BaseJudgeRunner",
    "CompositeVerifierEngine",
    "EvidenceBundle",
    "EvidenceCollector",
    "EvidenceRecord",
    "EvaluationContext",
    "EvaluationItem",
    "EvaluationPlan",
    "JudgeFamily",
    "JudgeResult",
    "JudgeRunner",
    "JudgeSpec",
    "JudgeVerdict",
    "LLMJudgeRunner",
    "PassRateScoringPolicy",
    "RuleJudgeRunner",
    "ScoringPolicy",
    "ScoreResult",
    "VERIFIER_LLM_ENV_PREFIX",
    "VerdictStatus",
    "VerifierLLMRoute",
    "VLMJudgeRunner",
    "verifier_llm_route_from_mapping",
    "validate_numeric_payload",
]
