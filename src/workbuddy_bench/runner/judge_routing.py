"""Helpers for routing configured judge models to verifier-side clients."""

from __future__ import annotations

from typing import Any

from workbuddy_bench.judge.core.routing import (
    VERIFIER_LLM_ENV_PREFIX,
    VerifierLLMRoute,
)
from workbuddy_bench.runner.resolve_manifest import manifest_connection_mode
from workbuddy_bench.runner.model_endpoints import openai_api_base_url


VerifierLLMEndpoint = VerifierLLMRoute


def proxy_root_from_manifest(manifest: dict[str, Any] | None) -> str:
    """Return the runtime proxy root URL back-filled by ``scripts/run.sh``."""
    if not manifest:
        return ""
    connection = manifest.get("connection") or {}
    return str(
        connection.get("harness_base_url")
        or connection.get("proxy_url")
        or manifest.get("proxy_url")
        or ""
    ).strip()


def _needs_verifier_side_llm_route(manifest: dict[str, Any]) -> bool:
    llm_judge = manifest.get("llm_judge") or {}
    if not llm_judge.get("enabled"):
        return False
    mode = str(llm_judge.get("mode") or "host_side")
    return mode == "in_container"


def verifier_side_llm_endpoint(
    manifest: dict[str, Any] | None,
) -> VerifierLLMRoute | None:
    """Resolve the verifier-side judge endpoint from a run manifest.

    Returns ``None`` when this dataset/job has no verifier-side LLM judge. Raises
    clear configuration errors when an enabled verifier-side judge cannot be
    routed through the job-local/shared proxy.
    """
    if not manifest:
        return None

    llm_judge = manifest.get("llm_judge") or {}
    if not _needs_verifier_side_llm_route(manifest):
        return None

    route_slug = str(llm_judge.get("model_slug") or "")
    if not route_slug:
        raise ValueError(
            "verifier-side llm_judge is enabled but no judge model slug was "
            "resolved. Configure llm_judge.model with a configs/models slug."
        )

    connection_mode = manifest_connection_mode(manifest)
    if connection_mode != "local_proxy":
        raise ValueError(
            "verifier-side llm_judge requires model_connection='local_proxy'; "
            f"got {connection_mode!r}."
        )

    proxy_root = proxy_root_from_manifest(manifest)
    if not proxy_root:
        raise ValueError(
            "verifier-side llm_judge uses model_connection='local_proxy', but "
            "the manifest has no connection.proxy_url/harness_base_url. Use "
            "scripts/run.sh so the proxy URL is back-filled before prepare_job "
            "runs."
        )

    params = llm_judge.get("params") or {}
    raw_max = params.get("max_output_tokens", params.get("max_tokens"))
    try:
        max_output_tokens = int(raw_max) if raw_max is not None else None
    except (TypeError, ValueError):
        max_output_tokens = None

    return VerifierLLMRoute(
        base_url=openai_api_base_url(proxy_root),
        api_key=route_slug,
        model=route_slug,
        max_output_tokens=max_output_tokens,
    )


def verifier_side_llm_env(
    manifest: dict[str, Any] | None,
) -> dict[str, str]:
    """Return ``WORKBUDDY_VERIFIER_LLM_*`` env for Harbor runtime config."""
    endpoint = verifier_side_llm_endpoint(manifest)
    return endpoint.to_env() if endpoint else {}


def in_container_verifier_llm_endpoint(
    manifest: dict[str, Any] | None,
) -> VerifierLLMRoute | None:
    """Backward-compatible alias for verifier-side LLM routing."""
    return verifier_side_llm_endpoint(manifest)


def in_container_verifier_llm_env(
    manifest: dict[str, Any] | None,
) -> dict[str, str]:
    """Backward-compatible alias for verifier-side LLM env routing."""
    return verifier_side_llm_env(manifest)
