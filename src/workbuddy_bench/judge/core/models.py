"""Dataset-neutral evaluation value objects.

These dataclasses are the Phase 1 contract layer for the verifier refactor. They
do not run tests, call models, aggregate scores, or know about web/code/office
task layouts. Later phases adapt dataset-specific behavior into these shapes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class VerdictStatus(StrEnum):
    """Normalized status vocabulary emitted by judges."""

    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"
    ABSTAIN = "abstain"
    BUILD_ERROR = "build_error"
    JUDGE_ERROR = "judge_error"


class JudgeFamily(StrEnum):
    """Top-level judge execution families used by CompositeVerifier."""

    RULE = "rule"
    LLM = "llm"
    VLM = "vlm"
    AGENT = "agent"


def _to_jsonable(value: Any) -> Any:
    """Convert common Python values to a JSON-serializable structure."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return {
            key: _to_jsonable(item)
            for key, item in value.__dict__.items()
            if not key.startswith("_")
        }
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _require_unit_interval(name: str, value: float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {number}")
    return number


def _default_score(status: VerdictStatus) -> float | None:
    if status == VerdictStatus.PASS:
        return 1.0
    if status in (
        VerdictStatus.FAIL,
        VerdictStatus.BUILD_ERROR,
        VerdictStatus.JUDGE_ERROR,
    ):
        return 0.0
    return None


def _normalize_judge_family(value: JudgeFamily | str | None) -> JudgeFamily:
    if value is None:
        return JudgeFamily.RULE
    return JudgeFamily(value)


@dataclass
class EvaluationContext:
    """Attempt-level context shared across evidence collectors and judges."""

    dataset_id: str
    task_id: str
    attempt_id: str | None = None
    workspace: str | None = None
    tests_dir: str | None = None
    verifier_dir: str | None = None
    container_paths: dict[str, str] = field(default_factory=dict)
    host_paths: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "dataset_id": self.dataset_id,
                "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "workspace": self.workspace,
                "tests_dir": self.tests_dir,
                "verifier_dir": self.verifier_dir,
                "container_paths": self.container_paths,
                "host_paths": self.host_paths,
                "env": self.env,
                "metadata": self.metadata,
            }
        )


@dataclass
class EvaluationItem:
    """Smallest unit that can receive a verdict."""

    id: str
    type: str = "check"
    label: str = ""
    criteria: str = ""
    category: str = ""
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("EvaluationItem.id is required")
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise TypeError("EvaluationItem.weight must be numeric")
        if not math.isfinite(float(self.weight)) or float(self.weight) < 0:
            raise ValueError("EvaluationItem.weight must be a finite non-negative number")
        self.weight = float(self.weight)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "id": self.id,
                "type": self.type,
                "label": self.label,
                "criteria": self.criteria,
                "category": self.category,
                "weight": self.weight,
                "metadata": self.metadata,
            }
        )


@dataclass
class JudgeSpec:
    """Declarative plan entry for one judge invocation."""

    name: str
    type: str
    item_ids: list[str] = field(default_factory=list)
    weight: float = 1.0
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    family: JudgeFamily | str = JudgeFamily.RULE

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("JudgeSpec.name is required")
        if not self.type:
            raise ValueError("JudgeSpec.type is required")
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise TypeError("JudgeSpec.weight must be numeric")
        if not math.isfinite(float(self.weight)) or float(self.weight) < 0:
            raise ValueError("JudgeSpec.weight must be a finite non-negative number")
        self.weight = float(self.weight)
        self.family = _normalize_judge_family(self.family)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "name": self.name,
                "type": self.type,
                "family": self.family,
                "item_ids": self.item_ids,
                "weight": self.weight,
                "config": self.config,
                "metadata": self.metadata,
            }
        )


@dataclass
class EvaluationPlan:
    """Dataset-neutral plan consumed by CompositeVerifier orchestration."""

    dataset_id: str
    task_id: str
    items: list[EvaluationItem] = field(default_factory=list)
    judges: list[JudgeSpec] = field(default_factory=list)
    evidence_requirements: list[str] = field(default_factory=list)
    scoring_policy: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "dataset_id": self.dataset_id,
                "task_id": self.task_id,
                "items": self.items,
                "judges": self.judges,
                "evidence_requirements": self.evidence_requirements,
                "scoring_policy": self.scoring_policy,
                "metadata": self.metadata,
            }
        )


@dataclass
class EvidenceRecord:
    """One upstream evidence artifact available to judges."""

    id: str
    type: str
    uri: str | None = None
    content: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("EvidenceRecord.id is required")
        if not self.type:
            raise ValueError("EvidenceRecord.type is required")

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "id": self.id,
                "type": self.type,
                "uri": self.uri,
                "content": self.content,
                "metadata": self.metadata,
            }
        )


@dataclass
class EvidenceBundle:
    """Collection of evidence records gathered before judging."""

    records: list[EvidenceRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def by_id(self) -> dict[str, EvidenceRecord]:
        return {record.id: record for record in self.records}

    def add(self, record: EvidenceRecord) -> None:
        if record.id in self.by_id():
            raise ValueError(f"duplicate evidence id: {record.id}")
        self.records.append(record)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable({"records": self.records, "metadata": self.metadata})


@dataclass
class JudgeVerdict:
    """One judge's verdict for one evaluation item."""

    item_id: str
    status: VerdictStatus | str
    judge_name: str
    judge_type: str
    score: float | None = None
    evidence_ids: list[str] = field(default_factory=list)
    reason: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("JudgeVerdict.item_id is required")
        if not self.judge_name:
            raise ValueError("JudgeVerdict.judge_name is required")
        if not self.judge_type:
            raise ValueError("JudgeVerdict.judge_type is required")
        status = VerdictStatus(self.status)
        self.status = status
        self.score = _require_unit_interval(
            "JudgeVerdict.score",
            _default_score(status) if self.score is None else self.score,
        )
        self.confidence = _require_unit_interval(
            "JudgeVerdict.confidence",
            self.confidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "item_id": self.item_id,
                "status": self.status,
                "judge_name": self.judge_name,
                "judge_type": self.judge_type,
                "score": self.score,
                "evidence_ids": self.evidence_ids,
                "reason": self.reason,
                "confidence": self.confidence,
                "metadata": self.metadata,
            }
        )


@dataclass
class JudgeResult:
    """Normalized output from one judge invocation."""

    judge_name: str
    judge_type: str
    status: VerdictStatus | str = VerdictStatus.PASS
    verdicts: list[JudgeVerdict] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    judge_family: JudgeFamily | str | None = None

    def __post_init__(self) -> None:
        if not self.judge_name:
            raise ValueError("JudgeResult.judge_name is required")
        if not self.judge_type:
            raise ValueError("JudgeResult.judge_type is required")
        self.status = VerdictStatus(self.status)
        if self.judge_family is not None:
            self.judge_family = _normalize_judge_family(self.judge_family)
        for key, value in self.scores.items():
            _require_unit_interval(f"JudgeResult.scores[{key!r}]", value)

    @property
    def ok(self) -> bool:
        return self.status not in (VerdictStatus.BUILD_ERROR, VerdictStatus.JUDGE_ERROR)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "judge_name": self.judge_name,
                "judge_type": self.judge_type,
                "judge_family": self.judge_family,
                "status": self.status,
                "ok": self.ok,
                "verdicts": self.verdicts,
                "scores": self.scores,
                "evidence_ids": self.evidence_ids,
                "errors": self.errors,
                "metadata": self.metadata,
            }
        )


@dataclass
class ScoreResult:
    """Final scoring output before artifact writing."""

    reward: float
    test_status: str = "no_pass"
    tests_passed: int | None = None
    tests_total: int | None = None
    verdicts: list[JudgeVerdict] = field(default_factory=list)
    judge_results: list[JudgeResult] = field(default_factory=list)
    numeric: dict[str, float | int] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.reward = _require_unit_interval("ScoreResult.reward", self.reward) or 0.0
        for name, value in (
            ("ScoreResult.tests_passed", self.tests_passed),
            ("ScoreResult.tests_total", self.tests_total),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    def _all_verdicts(self) -> list[JudgeVerdict]:
        verdicts = list(self.verdicts)
        for result in self.judge_results:
            verdicts.extend(result.verdicts)
        return verdicts

    def reward_payload(self) -> dict[str, float | int]:
        payload: dict[str, float | int] = dict(self.numeric)
        payload["reward"] = self.reward
        if self.tests_passed is not None:
            payload["tests_passed"] = self.tests_passed
        if self.tests_total is not None:
            payload["tests_total"] = self.tests_total
        return payload

    def score_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reward": self.reward,
            "test_status": self.test_status,
            **dict(self.numeric),
            "verdicts": [verdict.to_dict() for verdict in self._all_verdicts()],
            "judges": [result.to_dict() for result in self.judge_results],
            "diagnostics": _to_jsonable(self.diagnostics),
            "metadata": _to_jsonable(self.metadata),
        }
        payload["reward"] = self.reward
        if self.tests_passed is not None:
            payload["tests_passed"] = self.tests_passed
        if self.tests_total is not None:
            payload["tests_total"] = self.tests_total
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "reward": self.reward_payload(),
            "score": self.score_payload(),
        }
