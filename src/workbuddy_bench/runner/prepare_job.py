"""Compose Harbor job configs from the 4-layer config system.

A slim job YAML names a model, a harness, and a dataset; ``prepare_job``
deep-merges those with the bench-wide defaults to produce a full harbor
runtime config.

Layered sources (later wins on conflicts):

1. ``configs/bench/`` — harbor-runtime invariants (jobs_dir, n_attempts,
   top-level orchestrator fields ``n_concurrent_trials``/``quiet``, environment,
   llm_judge), resolved by ``bench_config.load_bench`` as
   ``_default.yaml`` deep-merged under the per-benchmark
   ``<dataset_id>.yaml``.
2. ``configs/harnesses/<family>/<version>.yaml`` — harness identity
   (import_path / built-in name), default ``harness.params`` + ``harness.env``.
3. ``configs/models/<model>.yaml`` — model identity (name / protocol /
   backend env vars), default inference + ``extra_body`` under ``model.params``.
4. ``configs/jobs/<job>.yaml`` — pure composition + per-job overrides
   (``orchestrator_override`` / ``environment_override`` /
   ``model_params_override`` / ``harness_params_override`` /
   ``env_override`` / ``jobs_dir_suffix``).

Output (written to ``--output-dir``):

* one ``agents:`` entry with the harness import_path
* ``model_name`` = model slug (the proxy route key)
* ``kwargs`` = {harness.params, ``model_params``}
* ``env`` = harness.env merged with per-job env_override
* harbor-level keys (n_concurrent_trials/quiet, environment, ...) lifted to job root
* ``datasets:`` — the harbor-facing dataset path list
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml

from workbuddy_bench.runner.bench_config import load_bench
from workbuddy_bench.runner.resolve_manifest import manifest_connection_mode
from workbuddy_bench.runner.config_loaders import (
    DatasetRuntimeContract,
    deep_merge,
    load_dataset_runtime_contract,
    load_harness_config,
    load_yaml,
    select_harness_mount_image,
    validate_harness_mount_available,
)
from workbuddy_bench.runner.harness_backends import (
    harbor_environment_for_harness_backend,
    resolve_harness_backend,
)
from workbuddy_bench.runner.judge_routing import verifier_side_llm_env


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _configs_dir() -> Path:
    override = os.environ.get("BENCH_CONFIGS_DIR")
    if override:
        return Path(override)
    return _repo_root() / "configs"


def _load_json_preset(ref: str, file_key: str) -> dict:
    """Load a harness JSON preset (settings.json / models.json) → parsed dict.

    Resolved relative to the repo root; doc-only keys (leading underscore) are
    stripped so they never reach the harness CLI.
    """
    path = Path(ref)
    if not path.is_absolute():
        path = _repo_root() / path
    if not path.is_file():
        raise FileNotFoundError(f"harness {file_key} not found: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: {file_key} preset must be a JSON object")
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _load_model(slug: str, configs_dir: Path) -> dict:
    """Return the ``model`` block from ``configs/models/<slug>.yaml``.

    Strict slug match only — no backend-id fallback. Ambiguity between
    models that share the same backend ``model.name`` is resolved by the
    slug in the job YAML.
    """
    path = configs_dir / "models" / f"{slug}.yaml"
    data = load_yaml(path)
    model = data.get("model")
    if not isinstance(model, dict):
        raise ValueError(f"{path}: missing top-level ``model:`` block")
    return model


def _load_harness(slug: str, configs_dir: Path) -> dict:
    return load_harness_config(slug, configs_dir).harness


def _load_bench(configs_dir: Path, dataset: str | None = None) -> dict:
    """Resolve the bench config (configs/bench/_default + per-benchmark) for ``dataset``."""
    return load_bench(configs_dir, dataset, repo_root=_repo_root())

def _harness_mount_volume(mount: dict, *, backend: str = "local") -> dict:
    """Translate a harness ``mount`` block into a Harbor ``ServiceVolumeConfig``.

    A docker-compose ``type: image`` volume mounts a read-only OCI image's
    filesystem into the container, preinstalling the harness CLI without baking
    it into every task image. Harness configs provide the image under
    ``mount.images.local``.
    """
    image = select_harness_mount_image(mount, backend)
    target = mount.get("path")
    if not image or not target:
        raise ValueError(
            f"harness ``mount`` requires both image for backend={backend!r} and ``path`` (got: {mount!r})"
        )
    return {
        "type": "image",
        "source": str(image),
        "target": str(target),
        "read_only": True,
    }


def _normalize_mounts(value: Any) -> list:
    """Coerce a ``mounts`` value into a list of mount dicts.

    Tolerates ``mounts`` being authored as a JSON-encoded string. A bare
    ``list(str)`` would split it character-by-character, producing junk
    ``mounts`` entries; parse it instead. Accepts a single mount object, a
    list, or ``None``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"environment.mounts is a string but not valid JSON: {exc}"
            ) from exc
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        normalized: list[dict] = []
        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(
                    "environment.mounts entries must each be a mapping; "
                    f"element {idx} is {type(item).__name__}"
                )
            normalized.append(item)
        return normalized
    raise ValueError(
        "environment.mounts must be a list (or JSON array string); "
        f"got {type(value).__name__}"
    )


def _build_agent_block(
    *, harness: dict, model_slug: str, model: dict, job: dict,
    manifest: dict[str, Any] | None = None,
) -> dict:
    """Assemble a single ``agents[*]`` entry from the layered sources."""
    harness_params = copy.deepcopy(harness.get("params") or {})
    harness_params = deep_merge(harness_params, job.get("harness_params_override") or {})

    # model.params holds inference scalars + (optionally) extra_body. The
    # wrappers consume the whole dict; when model_connection=local_proxy the
    # proxy injects extra_body, while direct-mode incompatibility is caught by
    # run.sh before execution. The wrapper records the requested params in
    # effective_config so the fingerprint reflects what was requested.
    model_params = copy.deepcopy(model.get("params") or {})
    model_params = deep_merge(model_params, job.get("model_params_override") or {})

    env = copy.deepcopy(harness.get("env") or {})
    env = deep_merge(env, job.get("env_override") or {})

    # Resolve the value the agent will send as the request's ``model``:
    #  * local_proxy: the proxy keys routes by slug, so address it with the route
    #    (``model_route``); the proxy rewrites ``body["model"]`` to the real
    #    backend name after the route lookup.
    #  * direct: there is nothing to rewrite the slug, so the agent must address
    #    the backend by its real model name.
    effective_model_name = model_slug
    effective_mode = "direct"
    proxy_url = ""
    if manifest:
        connection = manifest.get("connection") or {}
        effective_mode = manifest_connection_mode(manifest)
        if effective_mode == "local_proxy":
            effective_model_name = manifest.get("model_route", model_slug)
            proxy_url = str(connection.get("proxy_url") or connection.get("harness_base_url") or "")
        else:
            effective_model_name = manifest.get("backend_model_name") or model_slug

    agent: dict[str, Any] = {
        "import_path": harness["import_path"],
        "model_name": effective_model_name,
    }
    if env:
        agent["env"] = env
    kwargs: dict[str, Any] = dict(harness_params)
    if model_params:
        kwargs["model_params"] = model_params

    # Surface the trial's instance_id into the agent block. The agent itself does
    # not use it (cbc_agent pops it), but Harbor serializes agent.kwargs into the
    # trial's results/<trial>/config.json — making instance_id the offline key
    # that ties a trial directory back to proxy logs keyed by the same id.
    # (manifest carries instance_id at top level; see resolve_manifest.)
    if manifest and manifest.get("instance_id"):
        kwargs["instance_id"] = str(manifest["instance_id"])

    # Inject the resolved connection so the agent can decide how to address the
    # backend (e.g. cbc writes models.json pointing at the proxy URL under
    # local_proxy, or at CBC_BASE_URL under direct). The agent's ``model_name``
    # already carries the route slug in local_proxy mode.
    kwargs["connection"] = {
        "mode": effective_mode,
        "proxy_url": proxy_url,
        "model_route": effective_model_name if effective_mode == "local_proxy" else "",
    }

    # Pass the split-mount path so the agent's install() can link the mounted
    # harness launcher onto PATH at run time (task images are harness-free).
    harness_mount = harness.get("mount")
    if isinstance(harness_mount, dict) and harness_mount.get("path"):
        kwargs.setdefault("mount_path", str(harness_mount["path"]))

    # Resolve the deterministic harness config presets (e.g. cbc settings.json /
    # models.json) to their parsed content here (host side) and pass them as
    # kwargs, so the agent writes a known-good base into the container instead of
    # synthesizing config in Python. The agent overlays dynamic items on top.
    for file_key, preset_key in (("settings_file", "settings_preset"),
                                 ("models_file", "models_preset")):
        ref = harness.get(file_key)
        if ref and preset_key not in kwargs:
            kwargs[preset_key] = _load_json_preset(str(ref), file_key)
        # Never forward a host path into agent kwargs.
        kwargs.pop(file_key, None)

    # Context window + compaction percent (harness-agnostic pass-through). The
    # agent translates these into its own config — for cbc the translation is
    # version-dependent (route A writes maxInputTokens+pct; route B writes
    # CODEBUDDY_AUTO_COMPACT_WINDOW), see cbc_agent + config_loaders. Here we only
    # forward the resolved values. None → harness/CLI default.
    if manifest and isinstance(manifest.get("context_window"), dict):
        cw = manifest["context_window"]
        if cw.get("window") is not None:
            kwargs["context_window"] = int(cw["window"])
        if cw.get("compact_pct") is not None:
            kwargs["context_compact_pct"] = int(cw["compact_pct"])

    # Run-specific allowlist additions merged into the agent phase policy by
    # Harbor (kept as ALLOWLIST — never downgraded to public). Under local_proxy
    # + egress control the container reaches the model only via the host proxy
    # (host.docker.internal), so that host must be allowlisted or the sidecar
    # drops it. Ignored by Harbor for phases whose policy is already public.
    extra_allowed_hosts = job.get("extra_allowed_hosts")
    if extra_allowed_hosts:
        if not isinstance(extra_allowed_hosts, list):
            raise ValueError("job ``extra_allowed_hosts`` must be a list of hosts")
        agent["extra_allowed_hosts"] = [str(h) for h in extra_allowed_hosts]

    agent["kwargs"] = kwargs
    return agent


def _resolve_jobs_dir(bench: dict, job: dict, job_slug: str) -> str:
    if "jobs_dir" in job:
        return str(job["jobs_dir"])
    base = str(bench.get("jobs_dir", "results"))
    suffix = job.get("jobs_dir_suffix")
    if suffix:
        return f"{base}-{suffix}"
    # Default: namespace by job slug to avoid collisions.
    return f"{base}/{job_slug}"


def compose_job_config(
    job_path: Path, output_dir: Path, *,
    configs_dir: Path | None = None,
    manifest_path: Path | None = None,
    harness_backend: str | None = None,
) -> Path:
    """Compose the runtime harbor config and write it to ``output_dir``.

    Parameters
    ----------
    manifest_path
        Optional path to a resolved manifest JSON. When provided, model_route
        is injected into the agent block from the manifest.
    harness_backend
        Optional harness execution backend (``local``). Selects the Harbor
        runtime ``environment:`` base from runner-owned backend defaults.

    Returns the path to the freshly-written runtime YAML.
    """
    configs_dir = configs_dir or _configs_dir()
    job = load_yaml(job_path)
    job_slug = job_path.stem

    # Load manifest if provided
    manifest: dict[str, Any] | None = None
    if manifest_path and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())

    model_slug = job.get("model")
    harness_slug = job.get("harness")
    if not model_slug or not harness_slug:
        raise ValueError(
            f"{job_path}: job config must declare both ``model:`` and ``harness:`` keys"
        )

    dataset_ref = (manifest or {}).get("dataset") or job.get("dataset")
    bench = _load_bench(configs_dir, dataset_ref)
    model = _load_model(str(model_slug), configs_dir)
    harness = _load_harness(str(harness_slug), configs_dir)

    agent_block = _build_agent_block(
        harness=harness, model_slug=str(model_slug), model=model, job=job,
        manifest=manifest,
    )

    # Compose harbor-level config: start with bench defaults, merge job overrides.
    runtime: dict[str, Any] = copy.deepcopy(bench)
    runtime["jobs_dir"] = _resolve_jobs_dir(bench, job, job_slug)
    if "n_attempts" in job:
        runtime["n_attempts"] = job["n_attempts"]
    if "timeout_multiplier" in job:
        runtime["timeout_multiplier"] = job["timeout_multiplier"]
    if "verifier" in job:
        runtime["verifier"] = deep_merge(runtime.get("verifier") or {}, job["verifier"] or {})
    # Inject verifier-side judge routing for dataset verifiers that run their
    # own LLM rubric checks. ``llm_judge.mode: in_container`` means the dataset
    # verifier consumes WORKBUDDY_VERIFIER_LLM_*; ``host_side`` is reserved for
    # the separate post-run whitebox judge.
    judge_env = verifier_side_llm_env(manifest)
    if judge_env:
        verifier_block = runtime.get("verifier") or {}
        verifier_env = dict(verifier_block.get("env") or {})
        verifier_env.update(judge_env)  # judge-derived keys win
        verifier_block["env"] = verifier_env
        runtime["verifier"] = verifier_block

    # Orchestrator settings (``n_concurrent_trials`` / ``quiet`` / ``retry``) are
    # top-level runtime fields; bench config carries them at the runtime root via
    # the deepcopy above. Deep-merge the job's ``orchestrator_override`` onto the
    # top level.
    orch_override = dict(job.get("orchestrator_override") or {})
    for key, val in orch_override.items():
        if isinstance(val, dict) and isinstance(runtime.get(key), dict):
            runtime[key] = deep_merge(runtime[key], val)
        else:
            runtime[key] = val

    # Harness backend selection is a job-level choice (only local Docker is
    # supported), not a fifth YAML config layer. Harbor receives a
    # normal top-level ``environment:`` block; job ``environment_override``
    # deep-merges onto it.
    resolved_harness_backend = resolve_harness_backend(
        job,
        explicit=harness_backend,
        manifest=manifest,
        context=str(job_path),
    )
    env_base = harbor_environment_for_harness_backend(
        resolved_harness_backend,
        bench_environment=bench.get("environment"),
    )
    runtime["environment"] = deep_merge(
        env_base, job.get("environment_override") or {}
    )
    # ``mounts`` must be a list of mount objects. Normalize it: a job/env layer
    # may author it as a JSON-encoded string, and the downstream harness
    # split-mount block does ``list(mounts)``, which char-splits a bare string
    # and then calls ``.get("target")`` on a ``str`` (AttributeError). Coercing
    # once here keeps every consumer on a clean list.
    env = runtime["environment"]
    if "mounts" in env:
        env["mounts"] = _normalize_mounts(env.get("mounts"))
    # Required harness split-mount: mount the harness CLI as a read-only image
    # layer based on dataset runtime metadata and the selected harness config.
    dataset_for_runtime = (manifest or {}).get("dataset") or job.get("dataset") or ""
    dataset_runtime = DatasetRuntimeContract()
    if dataset_for_runtime:
        dataset_runtime = load_dataset_runtime_contract(dataset_for_runtime, repo_root=_repo_root())
    harness_name = harness.get("name", "")
    dataset_requires_mount = dataset_runtime.requires_split_mount_for(str(harness_name))
    harness_mount = harness.get("mount")
    if dataset_requires_mount:
        validate_harness_mount_available(
            harness,
            backend="local",
            context=f"{job_path}: dataset {dataset_for_runtime!r}",
        )
    if harness_mount and dataset_requires_mount:
        vol = _harness_mount_volume(harness_mount, backend="local")
        existing = list(runtime["environment"].get("mounts") or [])
        if not any(v.get("target") == vol["target"] for v in existing):
            existing.append(vol)
        runtime["environment"]["mounts"] = existing

    runtime["agents"] = [agent_block]

    dataset = (manifest or {}).get("dataset") or job.get("dataset")
    if not dataset:
        raise ValueError(f"{job_path}: must declare ``dataset:`` (path to tasks)")
    runtime["datasets"] = [{"path": str(dataset)}]

    # llm_judge config is read by the runner/post-judge pipeline, not Harbor.
    # Strip it and any legacy judge-env directive from the runtime YAML to keep
    # Harbor's view clean.
    runtime.pop("llm_judge", None)
    runtime.pop("llm_judge_override", None)
    runtime.pop("verifier_env_from_judge", None)

    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = output_dir / job_path.name
    runtime_path.write_text(
        yaml.safe_dump(runtime, sort_keys=False, allow_unicode=True)
    )
    return runtime_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compose a Harbor job config from WorkBuddy Bench's 4-layer configs."
    )
    parser.add_argument("config_path", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="Path to resolved manifest JSON (injects model_route)",
    )
    parser.add_argument(
        "--harness-backend", default=None,
        help="Harness execution backend (local); overrides job harness_backend.",
    )
    args = parser.parse_args()

    print(compose_job_config(
        args.config_path, args.output_dir,
        manifest_path=args.manifest,
        harness_backend=args.harness_backend,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
