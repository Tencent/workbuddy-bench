"""Harness execution backend selection for runner composition.

``harness_backend`` is a job-level choice of where the harness sandbox runs.
Only the local Docker backend is supported. The field is retained (rather
than removed) so the manifest schema and job YAML stay stable and a remote
backend can be reintroduced later without reshaping callers.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any


VALID_HARNESS_BACKENDS = {"local"}

LOCAL_HARBOR_ENVIRONMENT: dict[str, Any] = {
    "type": "docker",
    "import_path": "workbuddy_bench.runner.docker_environment:WorkBuddyDockerEnvironment",
    "force_build": True,
    "delete": False,
}


def _normalize(value: Any, *, source: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{source} must be a string backend slug (local); "
            "use environment_override: for Harbor environment field tweaks"
        )
    backend = value.strip()
    if backend not in VALID_HARNESS_BACKENDS:
        raise ValueError(
            f"{source}={value!r} is invalid; expected one of "
            f"{sorted(VALID_HARNESS_BACKENDS)}"
        )
    return backend


def _manifest_backend(manifest: Mapping[str, Any] | None) -> str | None:
    if not manifest:
        return None
    raw = manifest.get("harness_backend")
    if raw is None:
        return None
    return _normalize(raw, source="manifest harness_backend")


def _job_backend(job: Mapping[str, Any], *, context: str) -> str | None:
    raw = job.get("harness_backend")
    if raw is None:
        return None
    return _normalize(raw, source=f"{context}: job harness_backend")


def resolve_harness_backend(
    job: Mapping[str, Any],
    *,
    explicit: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    context: str = "job config",
) -> str:
    """Resolve the harness execution backend for a job.

    Precedence is explicit arg, manifest, ``BENCH_HARNESS_BACKEND_OVERRIDE``,
    job ``harness_backend``, then ``local``.
    """

    if explicit:
        explicit_backend = _normalize(explicit, source="explicit harness_backend")
        manifest_backend = _manifest_backend(manifest)
        if manifest_backend and manifest_backend != explicit_backend:
            raise ValueError(
                f"explicit harness_backend={explicit_backend!r} conflicts with "
                f"manifest harness_backend={manifest_backend!r}"
            )
        return explicit_backend

    manifest_backend = _manifest_backend(manifest)
    if manifest_backend:
        return manifest_backend

    env = environ if environ is not None else os.environ
    env_override = env.get("BENCH_HARNESS_BACKEND_OVERRIDE") or ""
    if env_override:
        return _normalize(env_override, source="BENCH_HARNESS_BACKEND_OVERRIDE")

    return _job_backend(job, context=context) or "local"


def harbor_environment_for_harness_backend(
    harness_backend: str,
    *,
    bench_environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the Harbor runtime ``environment:`` base for a harness backend."""

    _normalize(harness_backend, source="harness_backend")

    if bench_environment:
        if not isinstance(bench_environment, Mapping):
            raise ValueError("configs/bench: environment must be a mapping")
        return copy.deepcopy(dict(bench_environment))
    return copy.deepcopy(LOCAL_HARBOR_ENVIRONMENT)
