"""Harbor-backed pytest runner for core verifier profiles."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harbor.environments.base import BaseEnvironment

from workbuddy_bench.judge.core.models import (
    EvidenceBundle,
    EvaluationContext,
    EvaluationPlan,
    JudgeResult,
    JudgeSpec,
    VerdictStatus,
)
from workbuddy_bench.judge.core.judges import RuleJudgeRunner
from workbuddy_bench.judge.runtime.command import CommandResult, HarborCommandExecutor
from workbuddy_bench.judge.runners.rule.common import (
    command_from_config,
    errored_result,
    truncate,
)
from workbuddy_bench.judge.runners.rule.pytest_runner import _classify, _verdicts_from_junit
from workbuddy_bench.scorer.scorer import parse_junit_xml


def _host_path(context: EvaluationContext, key: str, fallback: Path | None) -> Path | None:
    raw = context.host_paths.get(key)
    if raw:
        return Path(raw)
    return fallback


@dataclass
class HarborPytestRuleJudgeRunner(RuleJudgeRunner):
    """Run pytest inside a Harbor environment and normalize JUnit XML.

    The runner intentionally mirrors :class:`PytestRuleJudgeRunner`'s output
    shape, but executes through ``BaseEnvironment.exec`` so it can be used from
    Harbor-facing profiles without blocking the active asyncio loop.
    """

    environment: BaseEnvironment

    @property
    def executor(self) -> HarborCommandExecutor:
        return HarborCommandExecutor(self.environment)

    async def run(
        self,
        context: EvaluationContext,
        plan: EvaluationPlan,
        evidence: EvidenceBundle,
        judge: JudgeSpec,
    ) -> JudgeResult:
        config: dict[str, Any] = dict(judge.config)
        cwd = str(config.get("cwd") or context.workspace or "/workspace")
        env = _merged_env(context.env, config.get("env") or {})
        timeout_sec = config.get("timeout_sec")

        setup_result = await self._run_setup_commands(
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

        raw_command = config.get("command")
        if raw_command is None:
            raise ValueError("HarborPytestRuleJudgeRunner requires config.command")
        command = command_from_config(raw_command)
        if not isinstance(command, str):
            raise TypeError("HarborPytestRuleJudgeRunner requires a shell command string")

        await self._cleanup_previous_results(context)
        result = await self._exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec)
        if result.timed_out or result.error:
            return errored_result(
                judge=judge,
                status=VerdictStatus.JUDGE_ERROR,
                message=result.error or "pytest command failed before producing a result",
                result=result,
                plan=plan,
            )

        await self._download_verifier_dir(context)

        host_verifier_dir = (
            Path(context.host_paths["verifier_dir"])
            if context.host_paths.get("verifier_dir")
            else None
        )
        junit_xml = _host_path(
            context,
            "junit_xml",
            host_verifier_dir / "results.xml" if host_verifier_dir else None,
        )
        output_path = _host_path(
            context,
            "test_output",
            host_verifier_dir / "test_output.txt" if host_verifier_dir else None,
        )
        if junit_xml is None:
            return errored_result(
                judge=judge,
                status=VerdictStatus.JUDGE_ERROR,
                message="host verifier directory is unavailable for pytest results",
                result=result,
                plan=plan,
            )
        file_output_text = (
            output_path.read_text(errors="replace")
            if output_path is not None and output_path.is_file()
            else ""
        )
        output_text = file_output_text or result.output_text

        passed, failed, errors = parse_junit_xml(str(junit_xml))
        total = passed + failed + errors
        rate = round(passed / total, 4) if total > 0 else 0.0
        status = _classify(result.return_code, total, passed, output_text)
        if status == "build_error":
            diagnostic_result = result
            if output_text and not result.output_text:
                diagnostic_result = CommandResult(
                    command=result.command,
                    return_code=result.return_code,
                    stdout=output_text,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                    error=result.error,
                    duration_sec=result.duration_sec,
                )
            return errored_result(
                judge=judge,
                status=VerdictStatus.BUILD_ERROR,
                message="pytest did not produce a valid test run",
                result=diagnostic_result,
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

    async def _run_setup_commands(
        self,
        *,
        commands: list[Any],
        cwd: str,
        env: dict[str, str],
        timeout_sec: float | None,
    ) -> CommandResult | None:
        for raw in commands:
            if isinstance(raw, Mapping):
                raw_command = raw.get("command")
                command_cwd = str(raw.get("cwd") or cwd)
                command_env = dict(env)
                command_env.update({str(k): str(v) for k, v in (raw.get("env") or {}).items()})
                command_timeout = raw.get("timeout_sec", timeout_sec)
            else:
                raw_command = raw
                command_cwd = cwd
                command_env = env
                command_timeout = timeout_sec
            command = command_from_config(raw_command)
            if not isinstance(command, str):
                raise TypeError("Harbor setup commands must be shell command strings")
            result = await self._exec(
                command,
                cwd=command_cwd,
                env=command_env,
                timeout_sec=command_timeout,
            )
            if result.return_code != 0 or result.timed_out or result.error:
                return result
        return None

    async def _exec(
        self,
        command: str,
        *,
        cwd: str,
        env: dict[str, str],
        timeout_sec: float | None,
    ) -> CommandResult:
        return await self.executor.run(
            command,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
        )

    async def _download_verifier_dir(self, context: EvaluationContext) -> None:
        if getattr(self.environment.capabilities, "mounted", False):
            return
        verifier_dir = context.verifier_dir
        host_verifier_dir = context.host_paths.get("verifier_dir")
        if verifier_dir and host_verifier_dir:
            await self.environment.download_dir(
                source_dir=verifier_dir,
                target_dir=Path(host_verifier_dir),
            )

    async def _cleanup_previous_results(self, context: EvaluationContext) -> None:
        host_verifier_dir = context.host_paths.get("verifier_dir")
        host_paths = [
            context.host_paths.get("junit_xml")
            or (str(Path(host_verifier_dir) / "results.xml") if host_verifier_dir else None),
            context.host_paths.get("test_output")
            or (str(Path(host_verifier_dir) / "test_output.txt") if host_verifier_dir else None),
        ]
        for raw_path in host_paths:
            if raw_path:
                try:
                    Path(raw_path).unlink(missing_ok=True)
                except OSError:
                    pass

        if not context.verifier_dir:
            return
        verifier_dir = str(context.verifier_dir).rstrip("/")
        await self.executor.run(
            "rm -f "
            + " ".join(
                shlex.quote(f"{verifier_dir}/{name}")
                for name in ("results.xml", "test_output.txt")
            ),
            cwd=context.workspace or "/workspace",
            env=context.env,
            timeout_sec=30,
        )


def _merged_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    env = {str(key): str(value) for key, value in base.items()}
    env.update({str(key): str(value) for key, value in extra.items()})
    return env
