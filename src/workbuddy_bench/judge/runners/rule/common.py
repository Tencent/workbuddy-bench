"""Shared helpers for deterministic rule judge runners."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from workbuddy_bench.judge.core.models import (
    EvaluationContext,
    EvaluationPlan,
    JudgeResult,
    JudgeSpec,
    JudgeVerdict,
    VerdictStatus,
)
from workbuddy_bench.judge.runtime.command import Command, CommandExecutor, CommandResult

_MAX_DIAGNOSTIC_TEXT = 8000


def truncate(text: str, limit: int = _MAX_DIAGNOSTIC_TEXT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def context_cwd(context: EvaluationContext, config: Mapping[str, Any]) -> Path:
    raw = config.get("cwd") or context.workspace or "."
    return Path(str(raw))


def context_env(context: EvaluationContext, config: Mapping[str, Any]) -> dict[str, str]:
    env = {str(key): str(value) for key, value in context.env.items()}
    env.update({str(key): str(value) for key, value in (config.get("env") or {}).items()})
    return env


def resolve_path(raw: str | Path, *, cwd: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else cwd / path


def command_from_config(value: Any) -> Command:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value]
    raise TypeError("command must be a string or sequence of strings")


def normalize_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value]
    raise TypeError("expected string or sequence of strings")


def run_setup_commands(
    *,
    executor: CommandExecutor,
    commands: Sequence[Any],
    cwd: Path,
    env: Mapping[str, str],
    timeout_sec: float | None,
) -> CommandResult | None:
    for raw in commands:
        if isinstance(raw, Mapping):
            command = command_from_config(raw.get("command"))
            command_cwd = resolve_path(raw.get("cwd", cwd), cwd=cwd)
            command_env = dict(env)
            command_env.update({str(k): str(v) for k, v in (raw.get("env") or {}).items()})
            shell = raw.get("shell")
            command_timeout = raw.get("timeout_sec", timeout_sec)
        else:
            command = command_from_config(raw)
            command_cwd = cwd
            command_env = env
            shell = None
            command_timeout = timeout_sec
        result = executor.run(
            command,
            cwd=command_cwd,
            env=command_env,
            timeout_sec=command_timeout,
            shell=shell,
        )
        if result.return_code != 0 or result.timed_out or result.error:
            return result
    return None


def errored_result(
    *,
    judge: JudgeSpec,
    status: VerdictStatus,
    message: str,
    result: CommandResult | None = None,
    plan: EvaluationPlan | None = None,
) -> JudgeResult:
    target_ids = judge.item_ids or ([item.id for item in plan.items] if plan is not None else [])
    verdicts = [
        JudgeVerdict(
            item_id=item_id,
            status=status,
            judge_name=judge.name,
            judge_type=judge.type,
            reason=message,
        )
        for item_id in target_ids
    ]
    metadata: dict[str, Any] = {"error": message}
    if result is not None:
        metadata["command"] = result.command
        metadata["return_code"] = result.return_code
        metadata["timed_out"] = result.timed_out
        metadata["stdout"] = truncate(result.stdout)
        metadata["stderr"] = truncate(result.stderr)
        metadata["duration_sec"] = result.duration_sec
    return JudgeResult(
        judge_name=judge.name,
        judge_type=judge.type,
        status=status,
        verdicts=verdicts,
        errors=[message],
        metadata=metadata,
    )


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def default_python_command() -> str:
    return sys.executable
