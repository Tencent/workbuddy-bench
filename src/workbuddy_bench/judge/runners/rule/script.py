"""Script-backed deterministic rule judge runner."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable

from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvaluationContext,
    EvaluationPlan,
    JudgeResult,
    JudgeSpec,
    VerdictStatus,
)
from workbuddy_bench.judge.core.judges import RuleJudgeRunner
from workbuddy_bench.judge.runtime.command import (
    CommandExecutor,
    HarborCommandExecutor,
    LocalCommandExecutor,
)
from workbuddy_bench.judge.runners.rule.common import (
    command_from_config,
    context_cwd,
    context_env,
    errored_result,
    truncate,
)
from workbuddy_bench.judge.runners.rule.reward_payload import (
    counts_from_payload,
    read_score_reward_payload,
    score_from_payload,
    status_from_payload,
    verdicts_from_payload,
)


def _judge_result_status(test_status: str) -> VerdictStatus:
    normalized = test_status.strip().lower()
    if normalized == "build_error":
        return VerdictStatus.BUILD_ERROR
    if normalized == "judge_error":
        return VerdictStatus.JUDGE_ERROR
    if normalized in {"pass", "full_pass"}:
        return VerdictStatus.PASS
    return VerdictStatus.FAIL


@dataclass
class ScriptRuleJudgeRunner(RuleJudgeRunner):
    """Run a deterministic script that emits a JSON score/reward payload."""

    executor: CommandExecutor = field(default_factory=LocalCommandExecutor)

    def run(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
    ) -> JudgeResult | Awaitable[JudgeResult]:
        config: dict[str, Any] = dict(judge.config)
        command = config.get("command")
        if command is None:
            return errored_result(
                judge=judge,
                status=VerdictStatus.JUDGE_ERROR,
                message="script rule judge requires config.command",
                plan=plan,
            )

        cwd = context_cwd(context, config)
        env = context_env(context, config)
        result = self.executor.run(
            command_from_config(command),
            cwd=cwd,
            env=env,
            timeout_sec=config.get("timeout_sec"),
            shell=config.get("shell"),
        )
        if inspect.isawaitable(result):
            return self._finish_async(result, context, plan, evidence, judge, config, cwd)
        return self._finish(result, context, plan, evidence, judge, config, cwd)

    async def _finish_async(
        self,
        result: Awaitable[Any],
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
        config: dict[str, Any],
        cwd: Any,
    ) -> JudgeResult:
        return self._finish(await result, context, plan, evidence, judge, config, cwd)

    def _finish(
        self,
        result: Any,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
        config: dict[str, Any],
        cwd: Any,
    ) -> JudgeResult:
        if result.timed_out or result.error:
            return errored_result(
                judge=judge,
                status=VerdictStatus.JUDGE_ERROR,
                message=result.error or "script command failed before producing a result",
                result=result,
                plan=plan,
            )

        read_result = read_score_reward_payload(context=context, cwd=cwd, config=config)
        payload = read_result.payload
        if payload is None:
            checked = ", ".join(path.as_posix() for path in read_result.checked_paths)
            detail = "; ".join(read_result.errors)
            message = "script did not write a valid score/reward JSON result"
            if checked:
                message += f" (checked: {checked})"
            if detail:
                message += f": {detail}"
            return errored_result(
                judge=judge,
                status=VerdictStatus.JUDGE_ERROR,
                message=message,
                result=result,
                plan=plan,
            )

        passed, total = counts_from_payload(payload)
        overall = score_from_payload(payload)
        rate = round(overall if overall is not None else (passed / total if total > 0 else 0.0), 4)
        status = status_from_payload(payload, passed, total)
        verdicts = verdicts_from_payload(
            judge=judge,
            payload=payload,
            passed=passed,
            total=total,
            item_ids=judge.item_ids or [item.id for item in plan.items],
        )
        scores = {"pass_rate": rate}
        if overall is not None:
            scores["overall"] = round(overall, 4)

        return JudgeResult(
            judge_name=judge.name,
            judge_type=judge.type,
            status=_judge_result_status(status),
            verdicts=verdicts,
            scores=scores,
            evidence_ids=[record.id for record in evidence.records],
            metadata={
                "test_status": status,
                "tests_passed": passed,
                "tests_total": total,
                "result_json": read_result.path.as_posix() if read_result.path else "",
                "result_source": read_result.source,
                "checked_result_paths": [
                    path.as_posix() for path in read_result.checked_paths
                ],
                "script_exit_code": result.return_code,
                "stdout": truncate(result.stdout),
                "stderr": truncate(result.stderr),
                "duration_sec": result.duration_sec,
                "raw": payload,
            },
        )


@dataclass(init=False)
class HarborScriptRuleJudgeRunner(ScriptRuleJudgeRunner):
    """Run script judges in Harbor and sync verifier artifacts before parsing."""

    runtime: Any

    def __init__(
        self,
        runtime: Any,
        executor: CommandExecutor | None = None,
    ) -> None:
        self.runtime = runtime
        self.executor = executor or HarborCommandExecutor(runtime.environment)

    async def _finish_async(
        self,
        result: Awaitable[Any],
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
        config: dict[str, Any],
        cwd: Any,
    ) -> JudgeResult:
        return await self._finish_after_sync(
            await result,
            context,
            plan,
            evidence,
            judge,
            config,
            cwd,
        )

    def _finish(
        self,
        result: Any,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
        config: dict[str, Any],
        cwd: Any,
    ) -> Awaitable[JudgeResult]:
        return self._finish_after_sync(result, context, plan, evidence, judge, config, cwd)

    async def _finish_after_sync(
        self,
        result: Any,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
        config: dict[str, Any],
        cwd: Any,
    ) -> JudgeResult:
        if not result.timed_out and not result.error:
            await self.runtime.download_verifier_dir()
        return super()._finish(result, context, plan, evidence, judge, config, cwd)
