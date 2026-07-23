"""Harbor-facing runtime helpers for dataset-native verifier profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harbor.models.trial.paths import EnvironmentPaths

from workbuddy_bench.judge.core import ArtifactWriter, EvaluationContext, ScoreResult


def merged_verifier_env(
    verifier: Any,
    base: Mapping[str, str] | None = None,
    *,
    prepend_pythonpath: str | None = None,
) -> dict[str, str]:
    """Merge task, verifier, and override env blocks in runtime precedence order."""

    env: dict[str, str] = {str(key): str(value) for key, value in (base or {}).items()}
    task_env = getattr(getattr(verifier.task, "config", None), "verifier", None)
    if task_env is not None:
        env.update({str(k): str(v) for k, v in getattr(task_env, "env", {}).items()})
    if getattr(verifier, "verifier_env", None):
        env.update({str(k): str(v) for k, v in verifier.verifier_env.items()})
    if getattr(verifier, "override_env", None):
        env.update({str(k): str(v) for k, v in verifier.override_env.items()})
    if prepend_pythonpath:
        env["PYTHONPATH"] = _prepend_path(prepend_pythonpath, env.get("PYTHONPATH"))
    return env


def _prepend_path(prefix: str, value: str | None) -> str:
    existing = str(value or "").strip()
    if not existing:
        return prefix
    parts = existing.split(":")
    if prefix in parts:
        return existing
    return f"{prefix}:{existing}"


@dataclass
class HarborAttemptRuntime:
    """Stable Harbor runtime contract shared by dataset profiles."""

    verifier: Any
    environment: Any
    tests_dir: str
    workspace: str
    container_verifier_dir: str
    host_verifier_dir: Path

    @classmethod
    def from_verifier(cls, verifier: Any) -> "HarborAttemptRuntime":
        environment = verifier.environment
        env_paths = EnvironmentPaths.for_os(environment.os)
        host_verifier_dir = verifier.trial_paths.verifier_dir
        host_verifier_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            verifier=verifier,
            environment=environment,
            tests_dir=str(env_paths.tests_dir),
            workspace=(env_paths.tests_dir.parent / "workspace").as_posix(),
            container_verifier_dir=str(env_paths.verifier_dir),
            host_verifier_dir=host_verifier_dir,
        )

    @property
    def task_id(self) -> str:
        return str(self.verifier.task.short_name)

    async def upload_tests(self) -> None:
        await self.environment.upload_dir(
            source_dir=self.verifier.task.paths.tests_dir,
            target_dir=self.tests_dir,
        )

    async def upload_dir(self, *, source_dir: Path, target_dir: str) -> None:
        await self.environment.upload_dir(source_dir=source_dir, target_dir=target_dir)

    async def download_verifier_dir(
        self,
        *,
        source_dir: str | None = None,
        target_dir: Path | None = None,
    ) -> None:
        if getattr(self.environment.capabilities, "mounted", False):
            return
        await self.environment.download_dir(
            source_dir=source_dir or self.container_verifier_dir,
            target_dir=target_dir or self.host_verifier_dir,
        )

    def env(
        self,
        base: Mapping[str, str] | None = None,
        *,
        prepend_pythonpath: str | None = None,
    ) -> dict[str, str]:
        return merged_verifier_env(
            self.verifier,
            base,
            prepend_pythonpath=prepend_pythonpath,
        )

    def context(
        self,
        *,
        dataset_id: str,
        task_id: str | None = None,
        container_paths: Mapping[str, str] | None = None,
        host_paths: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationContext:
        return EvaluationContext(
            dataset_id=dataset_id,
            task_id=task_id or self.task_id,
            workspace=self.workspace,
            tests_dir=self.tests_dir,
            verifier_dir=self.container_verifier_dir,
            container_paths={str(k): str(v) for k, v in (container_paths or {}).items()},
            host_paths={str(k): str(v) for k, v in (host_paths or {}).items()},
            env={str(k): str(v) for k, v in (env or {}).items()},
            metadata=dict(metadata or {}),
        )

    def artifact_writer(self) -> ArtifactWriter:
        return ArtifactWriter(
            reward_json_path=self.verifier.trial_paths.reward_json_path,
            score_json_path=self.host_verifier_dir / "score.json",
        )

    def write_score(self, score: ScoreResult) -> None:
        self.artifact_writer().write(score)
