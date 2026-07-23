"""Harbor glue for CompositeVerifier dataset plugins."""

from __future__ import annotations

from typing import Any

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from workbuddy_bench.judge.registry import (
    RegistryBuildContext,
    build_default_context,
    load_verifier_contract,
    load_verifier_registry,
    maybe_await,
)
from workbuddy_bench.judge.runtime import HarborAttemptRuntime

_REMOVED_LEGACY_KWARGS = {
    "aggregate",
    "capture_patch",
    "dataset_config_path",
    "judges",
    "sources",
}


class CompositeVerifier(BaseVerifier):
    """Run one verification attempt through a dataset CompositeVerifier plugin."""

    def __init__(
        self,
        *,
        profile: str | None = None,
        **base_kwargs: Any,
    ) -> None:
        legacy_kwargs = sorted(_REMOVED_LEGACY_KWARGS.intersection(base_kwargs))
        if legacy_kwargs:
            joined = ", ".join(legacy_kwargs)
            raise ValueError(
                "legacy CompositeVerifier source configuration is no longer "
                f"supported ({joined}); declare a dataset verifier contract instead"
            )
        if profile is not None:
            raise ValueError(
                "verifier.kwargs.profile is no longer supported; declare "
                "[verifier] engine = 'composite' in dataset.toml and provide "
                "shared/verifier/plugin.py"
            )
        super().__init__(**base_kwargs)

    async def verify(self) -> VerifierResult:
        return await _run_registry_verifier(self)


async def _run_registry_verifier(verifier: Any) -> VerifierResult:
    try:
        task_dir = verifier.task.paths.task_dir
    except AttributeError as exc:
        raise ValueError(
            "CompositeVerifier requires task.paths.task_dir to belong to a "
            "dataset with a CompositeVerifier contract"
        ) from exc
    contract = load_verifier_contract(task_dir)
    runtime = HarborAttemptRuntime.from_verifier(verifier)
    registry = load_verifier_registry(
        RegistryBuildContext(contract=contract, runtime=runtime, verifier=verifier)
    )
    if registry.custom_verify is not None:
        return await maybe_await(registry.custom_verify(verifier))

    await runtime.upload_tests()
    context = build_default_context(contract, runtime)
    if registry.prepare is not None:
        await maybe_await(registry.prepare(context))

    plan = await maybe_await(registry.plan_builder(context))
    score = await registry.engine().run(context, plan)
    if registry.finalize_score is not None:
        score = await maybe_await(registry.finalize_score(score, context, plan))
    runtime.write_score(score)
    return VerifierResult(rewards=score.reward_payload())
