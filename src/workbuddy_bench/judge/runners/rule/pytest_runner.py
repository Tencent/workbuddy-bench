"""Pytest-backed deterministic rule judge runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvaluationContext,
    EvaluationPlan,
    JudgeResult,
    JudgeSpec,
    JudgeVerdict,
    VerdictStatus,
)
from workbuddy_bench.judge.core.judges import RuleJudgeRunner
from workbuddy_bench.judge.runtime.command import CommandExecutor, LocalCommandExecutor
from workbuddy_bench.judge.runners.rule.common import (
    command_from_config,
    context_cwd,
    context_env,
    default_python_command,
    errored_result,
    normalize_tokens,
    resolve_path,
    run_setup_commands,
    truncate,
)
from workbuddy_bench.scorer.scorer import parse_junit_testcases, parse_junit_xml

_BUILD_ERROR_MARKERS = (
    "ERROR: found no collectors",
    "ImportError while loading conftest",
    "fixture '",
)


def _classify(exit_code: int, total: int, passed: int, output_text: str) -> str:
    rate = passed / total if total > 0 else 0.0
    if exit_code not in (0, 1) or total <= 0 or any(m in output_text for m in _BUILD_ERROR_MARKERS):
        return "build_error"
    if rate >= 1.0:
        return "full_pass"
    if rate > 0:
        return "partial_pass"
    return "no_pass"


def _case_verdict_status(status: str) -> VerdictStatus:
    if status == "pass":
        return VerdictStatus.PASS
    if status == "skipped":
        return VerdictStatus.ABSTAIN
    return VerdictStatus.FAIL


def _verdicts_from_junit(
    *,
    judge: JudgeSpec,
    xml_path: Path,
    passed: int,
    total: int,
) -> list[JudgeVerdict]:
    verdicts: list[JudgeVerdict] = []
    seen: dict[str, int] = {}
    for case in parse_junit_testcases(str(xml_path)):
        classname = case["classname"]
        name = case["name"]
        base = f"{classname}::{name}" if classname else name
        n = seen.get(base, 0)
        seen[base] = n + 1
        item_id = base if n == 0 else f"{base}#{n}"
        verdicts.append(
            JudgeVerdict(
                item_id=item_id,
                status=_case_verdict_status(case["status"]),
                judge_name=judge.name,
                judge_type=judge.type,
                reason=case.get("detail") or "",
            )
        )
    if verdicts or total <= 0:
        return verdicts

    # Some JUnit writers expose only suite attributes. Keep scoring possible by
    # synthesizing aggregate pass/fail items with deterministic ids.
    for index in range(total):
        verdicts.append(
            JudgeVerdict(
                item_id=f"{judge.name}::test::{index}",
                status=VerdictStatus.PASS if index < passed else VerdictStatus.FAIL,
                judge_name=judge.name,
                judge_type=judge.type,
            )
        )
    return verdicts


@dataclass
class PytestRuleJudgeRunner(RuleJudgeRunner):
    """Run pytest and normalize JUnit XML into core judge verdicts."""

    executor: CommandExecutor = field(default_factory=LocalCommandExecutor)
    # Fallback wall-clock cap when a judge config omits ``timeout_sec``. Without
    # it a hung test (deadlock, waiting on stdin, infinite loop) runs forever.
    # Sized to the longest known verifier budget (office-v2 = 1800s) so heavy
    # suites are not killed prematurely; per-judge config still overrides it.
    default_timeout_sec: float = 1800.0

    def run(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
    ) -> JudgeResult:
        config: dict[str, Any] = dict(judge.config)
        cwd = context_cwd(context, config)
        env = context_env(context, config)
        timeout_sec = config.get("timeout_sec")
        if timeout_sec is None:
            timeout_sec = self.default_timeout_sec

        setup_result = run_setup_commands(
            executor=self.executor,
            commands=config.get("setup_commands") or [],
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
        )
        if setup_result is not None:
            return errored_result(
                judge=judge,
                status=VerdictStatus.BUILD_ERROR,
                message="pytest setup command failed",
                result=setup_result,
                plan=plan,
            )

        junit_xml = resolve_path(config.get("junit_xml", "results.xml"), cwd=cwd)
        # Remove any JUnit XML left by a previous run in this cwd. Without this, a
        # pytest that aborts before writing XML (collection crash, OOM-kill,
        # segfault) leaves the stale file in place and parse_junit_xml reads the
        # old counts — scoring a broken run as the previous run's result.
        try:
            junit_xml.unlink(missing_ok=True)
        except OSError:
            pass
        raw_command = config.get("command")
        if raw_command is None:
            command = [
                str(config.get("python", default_python_command())),
                "-m",
                "pytest",
                *normalize_tokens(config.get("targets")),
                *normalize_tokens(config.get("pytest_args", "-v")),
                f"--junitxml={junit_xml}",
            ]
        else:
            command = command_from_config(raw_command)

        result = self.executor.run(
            command,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            shell=config.get("shell"),
        )
        if result.timed_out or result.error:
            return errored_result(
                judge=judge,
                status=VerdictStatus.JUDGE_ERROR,
                message=result.error or "pytest command failed before producing a result",
                result=result,
                plan=plan,
            )

        passed, failed, errors = parse_junit_xml(str(junit_xml))
        total = passed + failed + errors
        rate = round(passed / total, 4) if total > 0 else 0.0
        status = _classify(result.return_code, total, passed, result.output_text)
        if status == "build_error":
            return errored_result(
                judge=judge,
                status=VerdictStatus.BUILD_ERROR,
                message="pytest did not produce a valid test run",
                result=result,
                plan=plan,
            )

        verdicts = _verdicts_from_junit(
            judge=judge,
            xml_path=junit_xml,
            passed=passed,
            total=total,
        )
        return JudgeResult(
            judge_name=judge.name,
            judge_type=judge.type,
            verdicts=verdicts,
            scores={"pass_rate": rate},
            evidence_ids=[record.id for record in evidence.records],
            metadata={
                "test_status": status,
                "tests_passed": passed,
                "tests_total": total,
                "pytest_exit_code": result.return_code,
                "junit_xml": junit_xml.as_posix(),
                "stdout": truncate(result.stdout),
                "stderr": truncate(result.stderr),
                "duration_sec": result.duration_sec,
            },
        )
