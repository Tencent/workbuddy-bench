"""Dataset verifier contract loading and plugin registry support."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
import tomllib
import types
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbuddy_bench.judge.core import (
    CompositeVerifierEngine,
    EvidenceCollector,
    EvaluationContext,
    EvaluationPlan,
    JudgeRunner,
    ScoreResult,
    ScoringPolicy,
)

VERIFIER_SCHEMA = "workbuddy.verifier.v1"
VERIFIER_ENGINE = "composite"
DEFAULT_PLUGIN_RELATIVE_PATH = Path("shared") / "verifier" / "plugin.py"


class VerifierContractError(ValueError):
    """Raised when a dataset does not declare a valid verifier contract."""


class VerifierPluginError(RuntimeError):
    """Raised when a verifier plugin cannot be loaded or is malformed."""


@dataclass(frozen=True)
class VerifierContract:
    """Resolved dataset/task verifier contract.

    The contract describes stable dataset layout and the CompositeVerifier
    engine declaration. It intentionally does not expose dataset-internal item
    sources such as pytest, case JSON, or judge.yaml.
    """

    dataset_root: Path
    task_dir: Path
    dataset_toml_path: Path
    task_toml_path: Path
    dataset_id: str
    dataset_version: str
    verifier_schema: str
    verifier_engine: str
    plugin: str | None
    source_case: str
    dataset_config: Mapping[str, Any] = field(default_factory=dict)
    task_config: Mapping[str, Any] = field(default_factory=dict)
    verifier_config: Mapping[str, Any] = field(default_factory=dict)
    layout: Mapping[str, Any] = field(default_factory=dict)

    @property
    def shared_dir(self) -> Path:
        return self.dataset_root / "shared"

    @property
    def conventional_plugin_path(self) -> Path:
        return self.dataset_root / DEFAULT_PLUGIN_RELATIVE_PATH

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_root": self.dataset_root.as_posix(),
            "task_dir": self.task_dir.as_posix(),
            "dataset_toml_path": self.dataset_toml_path.as_posix(),
            "task_toml_path": self.task_toml_path.as_posix(),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "verifier_schema": self.verifier_schema,
            "verifier_engine": self.verifier_engine,
            "plugin": self.plugin,
            "source_case": self.source_case,
            "layout": dict(self.layout),
        }


@dataclass(frozen=True)
class RegistryBuildContext:
    """Inputs passed to a dataset plugin's ``build_registry`` function."""

    contract: VerifierContract
    runtime: Any | None = None
    verifier: Any | None = None


PlanBuilder = Callable[[EvaluationContext], EvaluationPlan | Awaitable[EvaluationPlan]]
PrepareHook = Callable[[EvaluationContext], None | Awaitable[None]]
ScoreFinalizer = Callable[
    [ScoreResult, EvaluationContext, EvaluationPlan],
    ScoreResult | Awaitable[ScoreResult],
]
CustomVerifier = Callable[[Any], Any | Awaitable[Any]]


@dataclass
class VerifierRegistry:
    """Dataset plugin registration consumed by the generic verifier adapter."""

    plan_builder: PlanBuilder
    scoring_policy: ScoringPolicy
    evidence_collectors: Sequence[EvidenceCollector] = field(default_factory=tuple)
    judge_runners: Mapping[str, JudgeRunner] = field(default_factory=dict)
    prepare: PrepareHook | None = None
    finalize_score: ScoreFinalizer | None = None
    custom_verify: CustomVerifier | None = None

    def engine(self) -> CompositeVerifierEngine:
        if self.scoring_policy is None:
            raise ValueError("VerifierRegistry requires scoring_policy to build an engine")
        return CompositeVerifierEngine(
            evidence_collectors=list(self.evidence_collectors),
            judge_runners=dict(self.judge_runners),
            scoring_policy=self.scoring_policy,
        )


async def maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def load_verifier_contract(task_dir: str | Path) -> VerifierContract:
    """Load and validate the CompositeVerifier contract for ``task_dir``."""

    resolved_task_dir = Path(task_dir).resolve()
    dataset_root = find_dataset_root(resolved_task_dir)
    dataset_toml = dataset_root / "dataset.toml"
    task_toml = resolved_task_dir / "task.toml"
    dataset_config = _read_toml(dataset_toml)
    task_config = _read_toml(task_toml)

    dataset = _table(dataset_config, "dataset", dataset_toml)
    verifier = _table(dataset_config, "verifier", dataset_toml)
    dataset_id = _required_str(dataset, "id", dataset_toml)
    dataset_version = _required_str(dataset, "version", dataset_toml)
    verifier_schema = _required_str(verifier, "schema", dataset_toml)
    verifier_engine = _required_str(verifier, "engine", dataset_toml)
    if verifier_schema != VERIFIER_SCHEMA:
        raise VerifierContractError(
            f"unsupported verifier schema {verifier_schema!r} in {dataset_toml}"
        )
    if verifier_engine != VERIFIER_ENGINE:
        raise VerifierContractError(
            f"unsupported verifier engine {verifier_engine!r} in {dataset_toml}"
        )

    metadata = task_config.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise VerifierContractError(f"[metadata] must be a TOML table in {task_toml}")
    source_case = str(metadata.get("source_case") or "").strip()
    if not source_case:
        raise VerifierContractError(f"task is missing metadata.source_case: {task_toml}")

    raw_plugin = verifier.get("plugin")
    plugin = None if raw_plugin is None else str(raw_plugin).strip() or None
    layout = dataset_config.get("layout") or {}
    if not isinstance(layout, Mapping):
        raise VerifierContractError(f"[layout] must be a TOML table in {dataset_toml}")

    return VerifierContract(
        dataset_root=dataset_root,
        task_dir=resolved_task_dir,
        dataset_toml_path=dataset_toml,
        task_toml_path=task_toml,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        verifier_schema=verifier_schema,
        verifier_engine=verifier_engine,
        plugin=plugin,
        source_case=source_case,
        dataset_config=dataset_config,
        task_config=task_config,
        verifier_config=verifier,
        layout=layout,
    )


def find_dataset_root(task_dir: Path) -> Path:
    """Return the nearest ancestor containing ``dataset.toml``."""

    for candidate in (task_dir, *task_dir.parents):
        if (candidate / "dataset.toml").is_file():
            return candidate
    raise VerifierContractError(f"could not find dataset.toml for task: {task_dir}")


def load_verifier_registry(context: RegistryBuildContext) -> VerifierRegistry:
    """Load the dataset plugin and build a validated registry."""

    builder = load_registry_builder(context.contract)
    registry = builder(context)
    if not isinstance(registry, VerifierRegistry):
        raise VerifierPluginError(
            "verifier plugin build_registry must return VerifierRegistry, "
            f"got {type(registry).__name__}"
        )
    if not registry.judge_runners and not registry.evidence_collectors:
        # Empty registries are valid only for tests with a scoring policy and an
        # empty plan, but they tend to hide plugin wiring mistakes in real tasks.
        pass
    return registry


def load_registry_builder(
    contract: VerifierContract,
) -> Callable[[RegistryBuildContext], VerifierRegistry]:
    """Resolve the ``build_registry`` callable for ``contract``."""

    if contract.plugin:
        module_ref, _, attr = contract.plugin.partition(":")
        if not module_ref or not attr:
            raise VerifierPluginError(
                "verifier.plugin must use 'module:function' format when configured "
                f"in dataset.toml, got {contract.plugin!r}"
            )
        module = importlib.import_module(module_ref)
        builder = getattr(module, attr, None)
    else:
        plugin_path = contract.conventional_plugin_path
        if not plugin_path.is_file():
            raise VerifierPluginError(
                "dataset declares CompositeVerifier but has no verifier plugin: "
                f"{plugin_path}"
            )
        module = _load_conventional_plugin_module(contract, plugin_path)
        builder = getattr(module, "build_registry", None)

    if not callable(builder):
        raise VerifierPluginError("verifier plugin does not expose callable build_registry")
    return builder


def build_default_context(contract: VerifierContract, runtime: Any) -> EvaluationContext:
    """Create the standard attempt context passed into plugin registries."""

    host_verifier_dir = getattr(runtime, "host_verifier_dir", None)
    host_paths = {
        "dataset_root": contract.dataset_root.as_posix(),
        "task_dir": contract.task_dir.as_posix(),
        "tests_dir": (contract.task_dir / "tests").as_posix(),
    }
    if host_verifier_dir is not None:
        host_paths["verifier_dir"] = Path(host_verifier_dir).as_posix()

    return runtime.context(
        dataset_id=contract.dataset_id,
        env=runtime.env(),
        host_paths=host_paths,
        metadata={
            "dataset_version": contract.dataset_version,
            "source_case": contract.source_case,
            "verifier_contract": contract.to_dict(),
        },
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise VerifierContractError(f"missing TOML file: {path}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise VerifierContractError(f"malformed TOML file: {path}") from exc


def _table(data: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise VerifierContractError(f"[{name}] must be a TOML table in {path}")
    return value


def _required_str(data: Mapping[str, Any], key: str, path: Path) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise VerifierContractError(f"missing required field {key!r} in {path}")
    return value


def _load_conventional_plugin_module(contract: VerifierContract, path: Path) -> types.ModuleType:
    slug = _plugin_slug(contract)
    root_name = "_workbuddy_dataset_verifier_plugins"
    dataset_pkg_name = f"{root_name}.{slug}"
    verifier_pkg_name = f"{dataset_pkg_name}.verifier"
    module_name = f"{verifier_pkg_name}.plugin"

    _ensure_package(root_name, [])
    _ensure_package(dataset_pkg_name, [contract.shared_dir])
    _ensure_package(verifier_pkg_name, [path.parent])
    sys.modules.pop(module_name, None)

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise VerifierPluginError(f"could not load verifier plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise VerifierPluginError(f"failed to import verifier plugin {path}: {exc}") from exc
    return module


def _ensure_package(name: str, paths: Sequence[Path]) -> None:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__package__ = name
        sys.modules[name] = module
    module.__path__ = [path.as_posix() for path in paths]  # type: ignore[attr-defined]


def _plugin_slug(contract: VerifierContract) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", contract.dataset_id).strip("_") or "dataset"
    digest = hashlib.sha1(contract.dataset_root.as_posix().encode()).hexdigest()[:10]
    return f"{base}_{digest}"
