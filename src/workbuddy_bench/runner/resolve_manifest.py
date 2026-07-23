"""Resolve a per-run manifest from the layered config system.

The manifest is a single JSON file that captures **all** resolved configuration
for one evaluation run instance.  Every downstream consumer (validate_model,
prepare_job, sharded_eval, harbor run, proxy) reads this manifest instead of
re-parsing YAML or relying on ambient shell environment variables.

Usage from shell (called by run.sh)::

    python3 -m workbuddy_bench.runner.resolve_manifest \
        --job-config configs/jobs/foo.yaml \
        --model-config configs/models/bar.yaml \
        --instance-id "foo-12345-1748012345" \
        --instance-dir scripts/logs/instances/foo-12345-1748012345 \
        [--force-proxy]

Writes ``manifest.json`` into ``--instance-dir`` and prints the path to stdout.
Exit code 0 = success, 1 = resolution failure.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from workbuddy_bench.runner.bench_config import dataset_id_for, load_bench
from workbuddy_bench.runner.config_loaders import (
    CBC_AUTOCOMPACT_WINDOW_MAX,
    CBC_AUTOCOMPACT_WINDOW_MIN,
    cbc_uses_autocompact_window_env,
    deep_merge,
    harness_mount_summary,
    harness_version,
    load_dataset_runtime_contract,
    load_harness_config,
    load_yaml,
    normalize_model_protocols,
    validate_harness_mount_available,
)
from workbuddy_bench.runner.harness_backends import resolve_harness_backend
from workbuddy_bench.runner.model_params import flatten_params

# Manifest structure version. Bump when the manifest shape changes so old
# manifests remain identifiable (readers can branch on it). See the provenance
# block — a manifest records which config files produced it AND under which
# schema, so a run stays auditable even after configs or this resolver evolve.
MANIFEST_SCHEMA_VERSION = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _sha256_file(path: Path) -> str:
    """sha256 of a file's bytes, hex. Assumes the file exists (caller checks)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_source(layer: str, path: Path) -> dict[str, Any]:
    """One provenance entry: which config file, its repo-relative path, its hash.

    The hash pins the EXACT content that produced this manifest, so a later edit
    to the same config file is detectable — a stored manifest whose recorded
    sha256 no longer matches the file on disk was resolved against a different
    version. Pure (reads the file, no other side effects) so it stays inside the
    pure resolver. ``path`` is made repo-relative when possible for portability.
    """
    try:
        rel = str(path.resolve().relative_to(_repo_root()))
    except ValueError:
        rel = str(path)
    return {"layer": layer, "path": rel, "sha256": _sha256_file(path)}


def _collect_config_sources(
    *,
    job_config_path: Path,
    model_config_path: Path,
    harness_version_path: Path,
    harness_defaults_path: Path | None,
    configs_dir: Path,
    dataset: str | Path | None,
) -> list[dict[str, Any]]:
    """Enumerate every config FILE that fed this manifest, each with its sha256.

    Mirrors the layered load in ``resolve_manifest``: job + model + harness
    (version file, plus the family ``_defaults.yaml`` when one applies) + bench
    ``_default`` + the per-dataset bench file when it exists. Harness paths come
    from the already-resolved ``ResolvedHarnessConfig`` rather than being
    re-derived here, so the recorded files match exactly what the loader read.
    Only files that exist are recorded — an absent per-dataset bench file means
    ``_default`` alone drove the bench layer, which the list then reflects.
    """
    sources: list[dict[str, Any]] = [
        _config_source("job", job_config_path),
        _config_source("model", model_config_path),
        _config_source("harness", harness_version_path),
    ]
    if harness_defaults_path is not None and harness_defaults_path.is_file():
        sources.append(_config_source("harness_defaults", harness_defaults_path))
    bench_dir = configs_dir / "bench"
    bench_default = bench_dir / "_default.yaml"
    if bench_default.is_file():
        sources.append(_config_source("bench_default", bench_default))
    ds_id = dataset_id_for(dataset, repo_root=_repo_root()) if dataset else ""
    if ds_id:
        ds_file = bench_dir / f"{ds_id}.yaml"
        if ds_file.is_file():
            sources.append(_config_source("bench_dataset", ds_file))
    return sources


# LLM judge execution modes. Both share the same config contract (enabled +
# mode + model slug); only the executor differs. See resolve_llm_judge.
_JUDGE_MODES = ("host_side", "in_container")


def resolve_llm_judge(
    configs_dir: Path,
    job: dict[str, Any],
    job_config_path: Path,
) -> dict[str, Any]:
    """Resolve post-pipeline LLM judge settings from bench + job layers."""
    bench = load_bench(configs_dir, job.get("dataset"), repo_root=_repo_root())

    base = bench.get("llm_judge") or {}
    override = job.get("llm_judge_override") or {}
    if not isinstance(base, dict):
        raise ValueError("configs/bench: 'llm_judge' must be a mapping")
    if not isinstance(override, dict):
        raise ValueError(
            f"{job_config_path}: 'llm_judge_override' must be a mapping"
        )

    resolved = deep_merge(base, override)
    resolved.setdefault("enabled", False)
    # The judge ``mode`` is part of the shared config contract: every benchmark
    # declares enabled + mode + model. Two execution modes exist (the contract is
    # the same, only the executor differs):
    #   host_side    — post-run judge over the host result dir (agent.patch /
    #                  instruction / tests). Code/patch quality. Default. Can be
    #                  re-run standalone against an existing results/ dir.
    #   in_container — the dataset's own verifier runs the judge inside the
    #                  verifier process (e.g. Web visual/rubric checks and
    #                  Office rubric checks). It is not re-run standalone because
    #                  the required verifier-side artifacts are tied to the trial.
    resolved.setdefault("mode", "host_side")

    if not isinstance(resolved.get("enabled"), bool):
        raise ValueError(
            f"{job_config_path}: resolved llm_judge.enabled must be a boolean"
        )
    if resolved.get("mode") not in _JUDGE_MODES:
        raise ValueError(
            f"{job_config_path}: resolved llm_judge.mode must be one of "
            f"{_JUDGE_MODES}; got {resolved.get('mode')!r}"
        )

    # Drop a legacy metadata-only label if present.
    resolved.pop("model_slug", None)

    # Resolve the judge endpoint from the model slug whenever a *real* slug is
    # configured — even when ``enabled`` is false. Two judge consumers share this
    # resolved block:
    #   * the host-side post-judge (sharded_eval) runs only when ``enabled`` is
    #     true (gated by should_run_llm_judge);
    #   * verifier-side judges consume the resolved local-proxy route in
    #     verifier env when ``enabled`` is true and ``mode`` is in_container.
    # So we resolve-but-don't-run: the placeholder ``<model-slug>`` (the bench
    # default) means "no judge configured" → leave the block inert.
    judge_slug = resolved.get("model")
    judge_slug = judge_slug.strip() if isinstance(judge_slug, str) else ""
    placeholder = judge_slug in ("", "<model-slug>")

    if placeholder:
        if resolved["enabled"]:
            raise ValueError(
                f"{job_config_path}: llm_judge.enabled is true but llm_judge.model "
                "(a configs/models/<slug>.yaml slug) is missing"
            )
        return resolved

    judge_model_path = configs_dir / "models" / f"{judge_slug}.yaml"
    if not judge_model_path.is_file():
        raise ValueError(
            f"{job_config_path}: llm_judge.model={judge_slug!r} does not resolve to "
            f"a model config at {judge_model_path}"
        )
    judge_model_data = load_yaml(judge_model_path)
    judge_model = judge_model_data.get("model")
    if not isinstance(judge_model, dict):
        raise ValueError(f"{judge_model_path}: missing top-level 'model:' block")

    backend_url_env = str(judge_model.get("backend_url_env") or "")
    backend_key_env = str(judge_model.get("backend_key_env") or "")
    judge_model_name = str(judge_model.get("name") or "")
    judge_params = judge_model.get("params") or {}
    if not isinstance(judge_params, dict):
        raise ValueError(f"{judge_model_path}: model.params must be a mapping")
    if not backend_url_env:
        raise ValueError(
            f"{judge_model_path}: judge model must define 'backend_url_env'"
        )
    if not judge_model_name:
        raise ValueError(f"{judge_model_path}: judge model must define 'name'")

    api_base = os.environ.get(backend_url_env, "")
    # The backend URL env is only required to be *populated* when the judge will
    # actually run (enabled). For resolve-but-don't-run configs an unset env is
    # tolerated; prepare_job/proxy_config still preserve the env var names so a
    # later run can route through the proxy once enabled.
    if resolved["enabled"] and not api_base.strip():
        raise ValueError(
            f"{job_config_path}: llm_judge enabled with model {judge_slug!r}, but its "
            f"backend URL env '{backend_url_env}' is unset/empty. Set it in .env or shell."
        )

    resolved["model_slug"] = judge_slug
    resolved["model"] = judge_model_name
    resolved["api_base"] = api_base.strip()
    resolved["api_base_env"] = backend_url_env
    resolved["api_key_env"] = backend_key_env
    resolved["params"] = copy.deepcopy(judge_params)
    return resolved


def _resolve_context_window(
    bench: dict[str, Any],
    job: dict[str, Any],
    model: dict[str, Any],
    job_config_path: Path,
) -> dict[str, Any]:
    """Resolve the context window + compaction percent (precedence: job > bench).

    ``model.context_window`` (optional) declares the model's PHYSICAL maximum; if
    set, the resolved window must not exceed it (fail-fast). The result is
    harness-agnostic — each agent translates it into its own env (cbc →
    CODEBUDDY_AUTO_COMPACT_WINDOW / CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE).
    """
    def _int(v: Any) -> int | None:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(
                f"{job_config_path}: context_window/compact_pct must be integers"
            )

    window = _int(job.get("context_window"))
    if window is None:
        window = _int(bench.get("context_window"))
    pct = _int(job.get("context_compact_pct"))
    if pct is None:
        pct = _int(bench.get("context_compact_pct"))

    physical_max = _int(model.get("context_window"))
    if window is not None and physical_max is not None and window > physical_max:
        raise ValueError(
            f"{job_config_path}: context_window={window} exceeds the model's "
            f"declared physical maximum context_window={physical_max}; "
            "lower it or raise the model's limit."
        )
    if pct is not None and not (1 <= pct <= 100):
        raise ValueError(
            f"{job_config_path}: context_compact_pct must be in 1..100 (got {pct})"
        )
    return {
        "window": window,            # None → harness/CLI default
        "compact_pct": pct,          # None → harness/CLI default
        "model_physical_max": physical_max,
    }


def _resolve_settings_preset(
    harness: dict[str, Any], configs_dir: Path, file_key: str = "settings_file"
) -> dict[str, Any]:
    """Load a harness JSON preset (settings_file / models_file); doc keys stripped.

    Returns {} if the harness declares no such file.
    """
    ref = harness.get(file_key)
    if not ref:
        return {}
    path = Path(str(ref))
    if not path.is_absolute():
        path = configs_dir.parent / path  # configs_dir is .../configs
    if not path.is_file():
        raise FileNotFoundError(f"harness {file_key} not found: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: {file_key} preset must be a JSON object")
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _build_harness_runtime_config(
    *,
    harness: dict[str, Any],
    configs_dir: Path,
    model: dict[str, Any],
    job: dict[str, Any],
    context_window: dict[str, Any],
    connection_mode: str,
    backend_url_env: str,
    backend_key_env: str,
    model_route: str,
    backend_model_name: str,
) -> dict[str, Any]:
    """Consolidate the FINAL harness runtime config into the manifest (auditable).

    This is the deterministic, sanitized view of what each harness will write
    into the container — so a run is reproducible/auditable from the manifest
    alone, without reading agent code. Secrets are NOT included: the model
    endpoint is referenced by env-var NAME, the api key is redacted.

    Each harness addresses its model differently (cbc writes ~/.codebuddy/
    models.json; cc reads ANTHROPIC_* env), so the audit is built per harness —
    dispatched on ``harness.name`` — and must MIRROR what the matching agent
    actually writes. The tests in test_cbc_compaction_route.py /
    test_cc_runtime_config.py pin each branch against its agent to catch drift.
    """
    harness_params = deep_merge(
        copy.deepcopy(harness.get("params") or {}),
        job.get("harness_params_override") or {},
    )
    model_params = deep_merge(
        copy.deepcopy(model.get("params") or {}),
        job.get("model_params_override") or {},
    )
    name = str(harness.get("name") or "")
    common = dict(
        harness=harness,
        configs_dir=configs_dir,
        harness_params=harness_params,
        model_params=model_params,
        context_window=context_window,
        connection_mode=connection_mode,
        backend_url_env=backend_url_env,
        backend_key_env=backend_key_env,
        model_route=model_route,
        backend_model_name=backend_model_name,
    )
    if name in ("cbc", "cbc-agent", "codebuddy-code"):
        return _build_cbc_runtime_config(**common)
    if name in ("cc", "claude-code"):
        return _build_cc_runtime_config(**common)
    return _build_generic_runtime_config(**common)


def _build_cbc_runtime_config(
    *,
    harness: dict[str, Any],
    configs_dir: Path,
    harness_params: dict[str, Any],
    model_params: dict[str, Any],
    context_window: dict[str, Any],
    connection_mode: str,
    backend_url_env: str,
    backend_key_env: str,
    model_route: str,
    backend_model_name: str,
) -> dict[str, Any]:
    """Audit block for cbc — mirrors cbc_agent.run() (models.json + CODEBUDDY_* env)."""
    # Final settings.json = preset + dynamic overlay. Mirrors cbc_agent.run():
    # an EXPLICIT thinking_enabled overrides the preset's alwaysThinkingEnabled;
    # when unset the preset default is left intact (the preset defaults it true,
    # so an explicit false must be written to turn thinking off for :nothink
    # models). Disabled tools (permissions.deny) already live in the preset file
    # (single source of truth), so no injection.
    settings_json = dict(_resolve_settings_preset(harness, configs_dir, "settings_file"))
    if "thinking_enabled" in model_params:
        settings_json["alwaysThinkingEnabled"] = bool(model_params["thinking_enabled"])

    # cbc env the agent will export. Mirrors cbc_agent.run()'s version fork
    # (route A/B — see config_loaders.cbc_uses_autocompact_window_env):
    #   route B (>= 2.103.4): window → CODEBUDDY_AUTO_COMPACT_WINDOW (absolute,
    #     clamped [100k,1M]); pct is inert and not emitted; maxInputTokens omitted.
    #   route A (< 2.103.4): window → models.json maxInputTokens (below); pct →
    #     CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE.
    # The static harness.env (tool gates / title-skip) is folded in first so the
    # audit shows every env the container actually receives.
    use_window_env = cbc_uses_autocompact_window_env(harness_version(harness))
    translated_env: dict[str, str] = {
        str(k): str(v) for k, v in (harness.get("env") or {}).items()
    }
    win = context_window.get("window")
    if use_window_env:
        if win is not None:
            clamped = max(CBC_AUTOCOMPACT_WINDOW_MIN,
                          min(CBC_AUTOCOMPACT_WINDOW_MAX, int(win)))
            translated_env["CODEBUDDY_AUTO_COMPACT_WINDOW"] = str(clamped)
    elif context_window.get("compact_pct") is not None:
        translated_env["CODEBUDDY_AUTOCOMPACT_PCT_OVERRIDE"] = str(context_window["compact_pct"])

    # Final models.json entry (sanitized) = static preset + dynamic fields. The
    # endpoint url/key are never inlined: under local_proxy the URL is the proxy
    # and key is a dummy; under direct they reference env-var names. maxInputTokens
    # is written only on route A (route B omits it so the env window is honoured).
    models_json = dict(_resolve_settings_preset(harness, configs_dir, "models_file"))
    if connection_mode == "local_proxy":
        models_json.update({"id": model_route, "name": model_route,
                            "url": "<proxy_url>", "apiKey": "<dummy:proxy>"})
    else:
        models_json.update({"id": backend_model_name, "name": backend_model_name,
                            "url_env": backend_url_env, "apiKey": "<redacted>",
                            "apiKey_env": backend_key_env or None})
    if win is not None and not use_window_env:
        models_json["maxInputTokens"] = int(win)
    # maxOutputTokens is written in both modes. Under local_proxy the proxy also
    # injects max_tokens into the body and overrides it; this models.json value is
    # the cbc-native fallback and is harmlessly superseded.
    mot = model_params.get("max_output_tokens")
    if mot is not None:
        models_json["maxOutputTokens"] = mot
    # Drop unresolved ${VAR} preset placeholders.
    models_json = {
        k: v for k, v in models_json.items()
        if not (isinstance(v, str) and v.startswith("${") and v.endswith("}"))
    }

    return {
        "harness": "cbc",
        "max_turns": harness_params.get("CBC_MAX_TURNS"),
        # Disabled tools live in settings_json.permissions.deny (shown there).
        "disallowed_tools": (settings_json.get("permissions") or {}).get("deny"),
        "settings_json": settings_json,        # final file written to container
        "translated_env": translated_env,      # harness-specific env the agent sets
        "models_json": models_json,            # final models.json entry, sanitized
        "model_params": model_params,          # sampling + extra_body (not secret)
    }


def _build_cc_runtime_config(
    *,
    harness: dict[str, Any],
    configs_dir: Path,
    harness_params: dict[str, Any],
    model_params: dict[str, Any],
    context_window: dict[str, Any],
    connection_mode: str,
    backend_url_env: str,
    backend_key_env: str,
    model_route: str,
    backend_model_name: str,
) -> dict[str, Any]:
    """Audit block for claude-code — mirrors cc_agent.run().

    claude addresses its model via ANTHROPIC_* env (NOT a models.json) and reads
    its tool deny list from ``$CLAUDE_CONFIG_DIR/settings.json``. Context-window
    compaction maps to CLAUDE_CODE_AUTO_COMPACT_WINDOW / CLAUDE_AUTOCOMPACT_PCT_
    OVERRIDE (claude's analogues of cbc's CODEBUDDY_* env). The endpoint is
    referenced by env-var NAME under direct and by ``<proxy_url>`` under
    local_proxy; the api key is never inlined.
    """
    # settings.json: deterministic preset (carries permissions.deny). Unlike cbc,
    # cc_agent does not overlay alwaysThinkingEnabled — thinking depth is the
    # CLAUDE_CODE_EFFORT_LEVEL env from the version preset, folded in below.
    settings_json = dict(_resolve_settings_preset(harness, configs_dir, "settings_file"))

    # env claude receives. Static harness.env (effort level / traffic-disable) is
    # folded in first, then the dynamic ANTHROPIC_* addressing + context window.
    # Mirrors cc_agent.run()'s env dict (minus the runtime-only IS_SANDBOX and the
    # model-alias pins, which depend on the resolved run_user and so are not
    # deterministic from the manifest alone).
    env: dict[str, str] = {
        str(k): str(v) for k, v in (harness.get("env") or {}).items()
    }
    if connection_mode == "local_proxy":
        env["ANTHROPIC_BASE_URL"] = "<proxy_url>"
        env["ANTHROPIC_API_KEY"] = "<dummy:proxy>"
        env["ANTHROPIC_MODEL"] = model_route
    else:
        env["ANTHROPIC_BASE_URL"] = f"${{{backend_url_env}}}" if backend_url_env else ""
        env["ANTHROPIC_API_KEY"] = "<redacted>"
        env["ANTHROPIC_MODEL"] = backend_model_name
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    mot = model_params.get("max_output_tokens")
    if mot is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(mot)
    win = context_window.get("window")
    if win is not None:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(win)
    if context_window.get("compact_pct") is not None:
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(context_window["compact_pct"])
    # Drop empty-valued keys.
    env = {k: v for k, v in env.items() if v}

    return {
        "harness": "cc",
        "max_turns": harness_params.get("CLAUDE_CODE_MAX_TURNS"),
        # Disabled tools live in settings_json.permissions.deny (shown there).
        "disallowed_tools": (settings_json.get("permissions") or {}).get("deny"),
        "settings_json": settings_json,        # final $CLAUDE_CONFIG_DIR/settings.json
        "translated_env": env,                 # ANTHROPIC_* + CLAUDE_* env claude sets
        "model_params": model_params,          # sampling + extra_body (not secret)
    }


def _build_generic_runtime_config(
    *,
    harness: dict[str, Any],
    configs_dir: Path,
    harness_params: dict[str, Any],
    model_params: dict[str, Any],
    context_window: dict[str, Any],
    connection_mode: str,
    backend_url_env: str,
    backend_key_env: str,
    model_route: str,
    backend_model_name: str,
) -> dict[str, Any]:
    """Fallback audit for an unrecognized harness — only the harness-agnostic view.

    We cannot mirror an agent we don't know, so we surface just the static harness
    env + settings preset + resolved knobs, tagged so the reader sees the harness
    was not specially handled.
    """
    settings_json = dict(_resolve_settings_preset(harness, configs_dir, "settings_file"))
    return {
        "harness": str(harness.get("name") or "unknown"),
        "note": "no harness-specific audit builder; showing static config only",
        "disallowed_tools": (settings_json.get("permissions") or {}).get("deny"),
        "settings_json": settings_json,
        "translated_env": {str(k): str(v) for k, v in (harness.get("env") or {}).items()},
        "harness_params": harness_params,
        "model_params": model_params,
    }


# Operator-visible connection modes:
#   direct        — harness talks to the model backend with no proxy.
#   local_proxy   — bench proxy on the host (host.docker.internal); local backend.
_VALID_MODEL_CONNECTIONS = ("direct", "local_proxy")


def manifest_connection_mode(manifest: dict[str, Any]) -> str:
    """Single read-path for a manifest's effective connection mode.

    The manifest carries the same connection mode in several places —
    ``connection.effective`` / ``connection.mode`` / top-level
    ``model_connection`` — which resolve_manifest keeps equal (see the assertion
    in ``resolve_manifest``). Consumers used to each read a different one
    (proxy_config read ``model_connection``, validate_model read
    ``connection.mode``, prepare_job read ``connection.effective``), so a future
    divergence between those fields would make them disagree. Route every reader
    through here instead.

    The precedence reads only the AUTHORITATIVE fields: ``connection.effective``
    (the resolved mode) then top-level ``model_connection`` (what the job asked
    for). ``connection.mode`` is deliberately NOT consulted — it is a derived
    display view that collapses every non-proxy mode to ``"direct"`` (see
    ``resolve_manifest``), so once ``effective`` is allowed to name a mode beyond
    proxy/direct (sandbox_proxy / multi-stage), ``mode`` would answer with the
    collapsed value. Today all three are equal so this changes nothing; it just
    removes the collapsed field from the read-path before it can mislead.
    """
    connection = manifest.get("connection") or {}
    return (
        connection.get("effective")
        or manifest.get("model_connection")
        or "direct"
    )


def _model_connection(job: dict[str, Any], job_config_path: Path) -> str:
    """Return the model connection mode requested by the job.

    Defaults to ``local_proxy``: runs through the bench proxy by default
    (protocol conversion / extra_body injection / logging). A
    job opts out with an explicit ``model_connection: direct``.
    """
    raw = job.get("model_connection", "local_proxy")
    if raw is None or raw == "":
        raw = "local_proxy"
    expected = ", ".join(repr(m) for m in _VALID_MODEL_CONNECTIONS)
    if not isinstance(raw, str):
        raise ValueError(
            f"{job_config_path}: 'model_connection' must be one of {expected}"
        )
    value = raw.strip()
    if value not in _VALID_MODEL_CONNECTIONS:
        raise ValueError(
            f"{job_config_path}: unsupported model_connection={value!r}; "
            f"expected one of {expected}"
        )
    return value


_SELECTION_MODES = {"all", "first", "last", "index", "random", "name"}


def _list_task_names(dataset: str) -> list[str]:
    """Return sorted task directory names under ``<repo_root>/<dataset>``.

    Returns an empty list when the dataset directory is absent (e.g. during a
    dry-run on a machine without the dataset checked out). Callers that need a
    concrete subset validate emptiness themselves so they can raise a clear
    error pointing at the missing dataset.
    """
    tasks_dir = Path(dataset)
    if not tasks_dir.is_absolute():
        tasks_dir = _repo_root() / dataset
    if not tasks_dir.is_dir():
        return []
    return sorted(p.name for p in tasks_dir.iterdir() if p.is_dir())


def _stage_dataset(dataset: str, instance_id: str) -> str:
    """Copy the dataset to a throwaway staging dir and return the staged path.

    ``prepare_tasks`` rewrites task files in place (``user=`` injection,
    compose host-gateway, composite verifier import path). Running against the
    real ``datasets/`` tree would permanently mutate checked-in files. Instead
    we stage the dataset under
    ``.workspace/tmp/staged/<instance-id>/`` and point ``manifest['dataset']``
    there, so the real tree stays read-only and ``git diff datasets/`` is empty
    after any run.

    We copy the dataset ROOT (the parent of the tasks dir), not just ``tasks/``:
    ``dataset.toml`` lives beside ``tasks/`` and the harness-mount loader finds
    it by walking parents, so a tasks-only copy would lose it and silently fall
    back to mount defaults.

    Returns a repo-relative path to the staged tasks dir. If the source dataset
    is absent (dry-run on a machine without it checked out), returns ``dataset``
    unchanged so the existing missing-dataset behavior is preserved.
    """
    orig = Path(dataset)
    if not orig.is_absolute():
        orig = _repo_root() / dataset
    if not orig.is_dir():
        return dataset  # absent: leave unchanged (dry-run / no checkout)

    staged_root = _repo_root() / ".workspace" / "tmp" / "staged" / instance_id
    shutil.rmtree(staged_root, ignore_errors=True)  # defensive: fresh base

    src_root = orig.parent
    dst_root = staged_root / src_root.name
    shutil.copytree(src_root, dst_root, symlinks=True)
    return str((dst_root / orig.name).relative_to(_repo_root()))


def resolve_task_selection(
    selection: Any,
    task_names: list[str],
    *,
    dataset: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Resolve a job's ``task_selection`` spec into a concrete task subset.

    ``selection`` is the raw ``task_selection:`` block from the job YAML (or
    ``None`` when absent). ``task_names`` is the sorted list of task directory
    names discovered under the dataset.

    Returns ``(normalized_spec, selected)`` where ``selected`` is a sorted list
    of task names. An **empty** ``selected`` means "no restriction" — run every
    task in the dataset (the default, backwards-compatible behavior).

    Supported modes::

        task_selection:
          mode: all                       # default — run everything
          mode: first   / count: 5        # first N (sorted)
          mode: last    / count: 5        # last N (sorted)
          mode: index   / indices: [0, 4] # by 0-based position
          mode: random  / count: 5        # random N (deterministic)
                          seed: 0          #   optional, defaults to 0
          mode: name    / names: [a, b]   # explicit task slugs

    Raises ``ValueError`` for malformed specs (unknown mode, missing/invalid
    parameters, out-of-range indices, unknown task names, or a selection that
    needs the dataset listing when the dataset directory is absent).
    """
    if selection is None:
        return {"mode": "all"}, []
    if not isinstance(selection, dict):
        raise ValueError(
            f"'task_selection' must be a mapping, got {type(selection).__name__}"
        )

    mode = selection.get("mode", "all")
    if not isinstance(mode, str) or mode not in _SELECTION_MODES:
        raise ValueError(
            f"task_selection.mode={mode!r} is invalid; "
            f"expected one of {sorted(_SELECTION_MODES)}"
        )

    if mode == "all":
        return {"mode": "all"}, []

    def _require_count() -> int:
        count = selection.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(
                f"task_selection.count must be a positive integer for mode={mode!r}"
            )
        return count

    def _require_dataset_listing() -> list[str]:
        if not task_names:
            raise ValueError(
                f"task_selection.mode={mode!r} needs to enumerate the dataset, "
                f"but no task directories were found under {dataset!r}"
            )
        return task_names

    if mode in {"first", "last"}:
        count = _require_count()
        names = _require_dataset_listing()
        chosen = names[:count] if mode == "first" else names[-count:]
        return (
            {"mode": mode, "count": count, "count_selected": len(chosen)},
            sorted(chosen),
        )

    if mode == "index":
        indices = selection.get("indices")
        if not isinstance(indices, list) or not indices:
            raise ValueError(
                "task_selection.indices must be a non-empty list for mode='index'"
            )
        names = _require_dataset_listing()
        chosen: list[str] = []
        for raw_idx in indices:
            if not isinstance(raw_idx, int) or isinstance(raw_idx, bool):
                raise ValueError(
                    f"task_selection.indices entries must be integers, got {raw_idx!r}"
                )
            if raw_idx < 0 or raw_idx >= len(names):
                raise ValueError(
                    f"task_selection.indices entry {raw_idx} is out of range "
                    f"(0..{len(names) - 1} for {len(names)} tasks)"
                )
            chosen.append(names[raw_idx])
        return (
            {
                "mode": "index",
                "indices": list(indices),
                "count_selected": len(set(chosen)),
            },
            sorted(set(chosen)),
        )

    if mode == "random":
        count = _require_count()
        seed = selection.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("task_selection.seed must be an integer")
        names = _require_dataset_listing()
        k = min(count, len(names))
        chosen = random.Random(seed).sample(names, k)
        return (
            {
                "mode": "random",
                "count": count,
                "seed": seed,
                "count_selected": len(chosen),
            },
            sorted(chosen),
        )

    # mode == "name"
    names_spec = selection.get("names")
    if not isinstance(names_spec, list) or not names_spec:
        raise ValueError(
            "task_selection.names must be a non-empty list for mode='name'"
        )
    for entry in names_spec:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"task_selection.names entries must be non-empty strings, got {entry!r}"
            )
    # Validate against the dataset listing when available; tolerate a missing
    # dataset (dry-run without the data) since names are explicit.
    if task_names:
        unknown = [n for n in names_spec if n not in task_names]
        if unknown:
            raise ValueError(
                f"task_selection.names references unknown tasks: {unknown}"
            )
    return (
        {"mode": "name", "names": list(names_spec), "count_selected": len(set(names_spec))},
        sorted(set(names_spec)),
    )


# Public API


def resolve_manifest(
    *,
    job_config_path: Path,
    model_config_path: Path,
    instance_id: str,
    force_proxy: bool = False,
    harness_backend: str | None = None,
    stage: bool = True,
) -> dict[str, Any]:
    """Resolve a complete manifest dict from the layered config system.

    This is a **pure computation** (no side effects beyond reading config files).
    All decision logic lives here so it can be unit-tested without touching disk
    beyond the input YAML files.

    Parameters
    ----------
    job_config_path
        Path to the job YAML (e.g. ``configs/jobs/foo.yaml``).
    model_config_path
        Path to the model YAML (e.g. ``configs/models/bar.yaml``).
    instance_id
        Unique identifier for this run instance.
    force_proxy
        Whether FORCE_PROXY=1 was set. This records a local-proxy requirement;
        callers must still request ``model_connection: local_proxy`` explicitly.

    Returns
    -------
    dict
        The fully-resolved manifest conforming to the documented schema.

    Raises
    ------
    FileNotFoundError
        If any referenced config file is missing.
    ValueError
        If config files have structural problems (missing keys, wrong types).
    """
    # Load configs
    job = load_yaml(job_config_path)
    job_slug = job_config_path.stem

    model_slug = job.get("model")
    harness_slug = job.get("harness")
    if not model_slug:
        raise ValueError(f"{job_config_path}: missing required 'model:' key")
    if not harness_slug:
        raise ValueError(f"{job_config_path}: missing required 'harness:' key")

    model_data = load_yaml(model_config_path)
    model = model_data.get("model")
    if not isinstance(model, dict):
        raise ValueError(f"{model_config_path}: missing or invalid 'model:' block")

    configs_dir = job_config_path.parent.parent  # configs/jobs/../ → configs/
    bench = load_bench(configs_dir, job.get("dataset"), repo_root=_repo_root())
    llm_judge = resolve_llm_judge(configs_dir, job, job_config_path)
    context_window = _resolve_context_window(bench, job, model, job_config_path)
    resolved_harness = load_harness_config(str(harness_slug), configs_dir)
    harness = resolved_harness.harness

    # Extract fields
    model_name = model.get("name", "")
    model_protocols = normalize_model_protocols(model)
    backend_url_env = model.get("backend_url_env", "")
    backend_key_env = model.get("backend_key_env", "")
    # model_inject: the full set the proxy injects into the request body —
    # top-level sampling params (e.g. temperature) + extra_body contents, minus
    # reserved keys (thinking_enabled/max_output_tokens go to cbc settings.json/
    # models.json, not the body). This is what makes top-level params take effect.
    model_inject = flatten_params(model.get("params") or {})
    # model_extra_body: only the explicit extra_body sub-block — the params cbc
    # cannot forward natively (top_p/top_k/...), which therefore *require* the
    # proxy. Drives has_extra_params / proxy_required below. Kept distinct from
    # model_inject because top-level temperature is forwarded natively by cbc in
    # direct mode (cbc_agent writes models.json), so it must not mark a direct
    # job as proxy-required.
    model_extra_body = (model.get("params") or {}).get("extra_body")

    harness_name = harness.get("name", "")
    harness_protocol = harness.get("protocol", "openai")

    job_dataset = job.get("dataset", "")
    if not job_dataset:
        raise ValueError(f"{job_config_path}: missing required top-level 'dataset:' key")

    job_extra_body = (job.get("model_params_override") or {}).get("extra_body")

    # Resolve dataset
    dataset = job_dataset
    dataset_source = "job.dataset"

    # Retained for manifest schema compatibility. The runner no longer supports
    # ambient or CLI dataset overrides.
    dataset_override_warning: str | None = None

    # Stage the dataset to a throwaway copy so the in-place prepare_tasks edits
    # never touch the checked-in datasets/ tree. Must happen BEFORE the runtime
    # contract load + task enumeration below so everything downstream (those
    # calls + the manifest's "dataset", read by prepare_tasks, sharded_eval and
    # prepare_job) consistently points at the copy.
    dataset_staged_from = dataset
    if stage:
        dataset = _stage_dataset(dataset, instance_id)

    # Resolve harness backend + dataset runtime contract
    resolved_harness_backend = resolve_harness_backend(
        job,
        explicit=harness_backend,
        context=str(job_config_path),
    )

    dataset_runtime = load_dataset_runtime_contract(dataset, repo_root=_repo_root())
    dataset_requires_mount = dataset_runtime.requires_split_mount_for(harness_name)
    backend_for_mount = "local"
    if dataset_requires_mount:
        validate_harness_mount_available(
            harness,
            backend=backend_for_mount,
            context=f"{job_config_path}: dataset {dataset!r}",
        )
    harness_mount = harness_mount_summary(
        harness,
        backend=backend_for_mount,
        required=dataset_requires_mount,
    )

    # Resolve task selection
    # Optional ``task_selection:`` lets a job run a subset of the dataset
    # (by index / first-N / random-N / explicit names) without carving out a
    # separate dataset directory. ``selected_tasks == []`` means no restriction.
    # We only enumerate the dataset directory when a real subset is requested —
    # ``mode: all`` (and the no-selection default) skip the listing entirely.
    selection_spec = job.get("task_selection")
    selection_mode = (
        selection_spec.get("mode", "all")
        if isinstance(selection_spec, dict)
        else None
    )
    needs_listing = bool(selection_spec) and selection_mode != "all"
    task_listing = _list_task_names(dataset) if needs_listing else []
    try:
        task_selection, selected_tasks = resolve_task_selection(
            selection_spec, task_listing, dataset=dataset
        )
    except ValueError as exc:
        raise ValueError(f"{job_config_path}: {exc}") from exc

    # Resolve request_overrides
    # Base = the model's flattened params (top-level sampling params + extra_body
    # contents); the job-level extra_body override is deep-merged on top. Stored
    # under "extra_body" (the proxy's injection key) regardless of how it was
    # sourced, so proxy_config.py / the inject_extra_body interceptor are unchanged.
    request_overrides: dict[str, Any] = {}
    merged_extra_body: dict[str, Any] = copy.deepcopy(model_inject)
    if job_extra_body:
        merged_extra_body = deep_merge(merged_extra_body, job_extra_body)
    if merged_extra_body:
        request_overrides["extra_body"] = merged_extra_body

    # Resolve explicit model connection
    # ``model_connection`` is the operator-visible mode switch. We deliberately
    # do not auto-select proxy mode from protocol mismatch or extra_body; direct
    # mode records those requirements so run.sh can fail fast with a clear error.
    model_connection = _model_connection(job, job_config_path)
    uses_local_proxy = model_connection == "local_proxy"
    uses_proxy = uses_local_proxy
    # Opt-in full request/response logging in the proxy, keyed per trial. Default
    # off (no large shared log file). When on under local_proxy, the route is made
    # instance-specific so the proxy can tag each request with this trial's id.
    record_full_io = bool(job.get("record_full_io", False))
    needs_proxy_protocol = harness_protocol not in model_protocols
    # Only the explicit extra_body sub-block (model + job) requires the proxy:
    # those params (top_p/top_k/min_p/...) cannot be forwarded by cbc natively.
    # Top-level params that cbc DOES forward natively in direct mode — temperature
    # and max_output_tokens — must not count here, or a direct job declaring only
    # those would be wrongly flagged proxy-required. They are injected by the proxy
    # under local_proxy and carried by cbc's models.json under direct.
    has_extra_params = bool(model_extra_body) or bool(job_extra_body)
    # "proxy_required" records the requirements the host proxy must satisfy.
    # run.sh fail-fasts when a direct job needs any of these.
    proxy_required = (
        needs_proxy_protocol or has_extra_params or force_proxy
    )
    # Back-compat alias kept for existing manifest consumers/tests.
    local_proxy_required = proxy_required
    needs_proxy = uses_proxy  # back-compat field: requested/effective proxy use

    # Determine proxy reason for audit trail. This describes the explicit mode,
    # not an implicit routing decision.
    if uses_proxy:
        mode_label = model_connection
        if needs_proxy_protocol:
            proxy_reason = (
                f"model_connection={mode_label}; protocol bridge requested "
                f"(harness_protocol={harness_protocol}, "
                f"model_protocols={', '.join(model_protocols)})"
            )
        elif has_extra_params:
            proxy_reason = f"model_connection={mode_label}; extra_body injection requested"
        elif force_proxy:
            proxy_reason = f"model_connection={mode_label}; FORCE_PROXY=1"
        else:
            proxy_reason = f"model_connection={mode_label}"
    elif needs_proxy_protocol:
        proxy_reason = (
            "model_connection=direct; harness protocol not supported by model service "
            f"(harness_protocol={harness_protocol}, "
            f"model_protocols={', '.join(model_protocols)})"
        )
    elif has_extra_params:
        proxy_reason = "model_connection=direct; extra_body injection is unsupported"
    elif force_proxy:
        proxy_reason = "model_connection=direct; FORCE_PROXY=1 is incompatible"
    else:
        proxy_reason = ""

    # Resolve route
    # Instance-specific routes are needed when (a) a job-level override changes the
    # proxy route's request shape, or (b) record_full_io is on, so the proxy can
    # tag each request with this trial's instance_id (the route slug carries it).
    # Direct runs still record request_overrides, but address the backend by its
    # real model name instead of an internal route key.
    if uses_local_proxy and (job_extra_body or record_full_io):
        model_route = f"{instance_id}__{model_slug}"
        route_type = "instance-specific"
    else:
        model_route = str(model_slug)
        route_type = "shared"

    # Resolve backend URL (for informational purposes)
    backend_url = ""
    if backend_url_env:
        backend_url = os.environ.get(backend_url_env, "")

    # Consolidate final harness runtime config (auditable, sanitized)
    harness_runtime_config = _build_harness_runtime_config(
        harness=harness,
        configs_dir=configs_dir,
        model=model,
        job=job,
        context_window=context_window,
        connection_mode=model_connection,
        backend_url_env=backend_url_env,
        backend_key_env=backend_key_env,
        model_route=model_route,
        backend_model_name=model_name,
    )

    # Provenance: which config files (with content hashes) produced this
    # manifest, so a stored run stays traceable to its exact inputs even after
    # the configs are later edited. ``git`` and ``resolved_at`` are non-pure
    # (subprocess / wall-clock) and are filled by resolve_and_write; the pure
    # resolver leaves them None so it stays unit-testable and deterministic.
    config_sources = _collect_config_sources(
        job_config_path=job_config_path,
        model_config_path=model_config_path,
        harness_version_path=resolved_harness.path,
        harness_defaults_path=resolved_harness.defaults_path,
        configs_dir=configs_dir,
        dataset=job_dataset,
    )

    # Assemble manifest
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        # Provenance (config_sources is pure; git/resolved_at filled by writer)
        "provenance": {
            "config_sources": config_sources,
            "git": None,
            "resolved_at": None,
        },
        # Identity
        "instance_id": instance_id,
        "job_slug": job_slug,
        # Dataset
        "dataset": dataset,
        "dataset_source": dataset_source,
        "dataset_runtime": dataset_runtime.to_manifest(repo_root=_repo_root()),
        "task_selection": task_selection,
        "selected_tasks": selected_tasks,
        # Model & routing
        "model_slug": str(model_slug),
        "model_route": model_route,
        "backend_model_name": model_name,
        "backend_url": backend_url,
        "backend_key_env": backend_key_env,
        # Protocol & model connection
        "model_protocols": model_protocols,
        "harness_slug": str(harness_slug),
        "harness_resolved_slug": resolved_harness.slug,
        "harness_name": harness_name,
        "harness_protocol": harness_protocol,
        "harness_mount": harness_mount,
        "harness_backend": resolved_harness_backend,
        "model_connection": model_connection,
        "needs_proxy": needs_proxy,
        # Opt-in full request/response logging in the proxy (per-trial). Read by
        # proxy_config to enable logging + carry the route's instance_id.
        "record_full_io": record_full_io,
        "proxy_url": None,  # back-compat; filled by run.sh when a proxy is used
        "connection": {
            "phase": "resolved",
            "requested": model_connection,
            "effective": model_connection,
            "backend": resolved_harness_backend,
            "harness_backend": resolved_harness_backend,
            "mode": model_connection if uses_proxy else "direct",
            "uses_proxy": uses_proxy,
            "proxy_location": "host" if uses_local_proxy else None,
            "proxy_url": None,
            "harness_base_url": None,
            "harness_base_url_scope": None,
            "model_route": model_route,
            "upstream_backend_url_env": backend_url_env or None,
            "upstream_backend_key_env": backend_key_env or None,
        },
        "model_connection_requirements": {
            "protocol_bridge": needs_proxy_protocol,
            "extra_body_injection": has_extra_params,
            "force_proxy": force_proxy,
            "local_proxy_required": local_proxy_required,
            "proxy_required": proxy_required,
        },
        # Request overrides
        "request_overrides": request_overrides,
        # Context window + compaction (harness-agnostic; agent translates to env)
        "context_window": context_window,
        # Final harness runtime config written into the container (sanitized,
        # auditable: settings.json, translated env, models.json shape, knobs).
        "harness_runtime_config": harness_runtime_config,
        # Post-pipeline configuration
        "llm_judge": llm_judge,
        # Decisions audit trail
        "decisions": {
            "proxy_reason": proxy_reason,
            "route_type": route_type,
            "dataset_override_warning": dataset_override_warning,
            "dataset_staged_from": dataset_staged_from if stage else None,
            "dataset_staged_to": dataset if stage else None,
        },
    }

    # The authoritative connection mode is duplicated across connection.effective
    # and top-level model_connection. They must stay equal at the resolve boundary
    # so the single read-path (manifest_connection_mode) and the per-consumer reads
    # can never disagree; assert here at parse time rather than letting a downstream
    # reader silently pick the wrong one.
    #
    # connection.mode is deliberately excluded from this invariant: it is a derived
    # display view that collapses every non-proxy mode to "direct", so it is
    # expected to diverge from effective once effective names a richer mode.
    # manifest_connection_mode does not read it, so that divergence is harmless.
    _conn = manifest["connection"]
    if _conn["effective"] != manifest["model_connection"]:
        raise RuntimeError(
            "authoritative connection mode fields diverged: "
            f"effective={_conn['effective']!r} "
            f"model_connection={manifest['model_connection']!r}"
        )

    return manifest


def _git_provenance() -> dict[str, Any] | None:
    """Best-effort git state (commit + dirty flag) for the manifest.

    Non-pure (shells out to git), so it lives here in the writer, not in the
    pure resolver. Returns None when git is unavailable or the tree is not a
    repo — provenance degrades gracefully rather than failing a run.
    """
    root = str(_repo_root())
    try:
        commit = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return {"commit": commit, "dirty": bool(status.strip())}


def resolve_and_write(
    *,
    instance_dir: Path,
    job_config_path: Path,
    model_config_path: Path,
    instance_id: str,
    force_proxy: bool = False,
    harness_backend: str | None = None,
    stage: bool = True,
    resolved_at: str | None = None,
) -> Path:
    """Resolve manifest and write it to ``instance_dir/manifest.json``.

    Fills the non-pure provenance fields the resolver left None: git state
    (commit + dirty) and ``resolved_at``. ``resolved_at`` is injected by the
    caller (Date.now-style clocks are not always available); when omitted it is
    left None rather than stamped here. Returns the path to the written manifest.
    """
    manifest = resolve_manifest(
        job_config_path=job_config_path,
        model_config_path=model_config_path,
        instance_id=instance_id,
        force_proxy=force_proxy,
        harness_backend=harness_backend,
        stage=stage,
    )

    provenance = manifest.setdefault("provenance", {})
    provenance["git"] = _git_provenance()
    provenance["resolved_at"] = resolved_at

    instance_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = instance_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    return manifest_path


# CLI


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a per-run manifest from layered configs."
    )
    parser.add_argument(
        "--job-config", type=Path, required=True,
        help="Path to the job YAML (e.g. configs/jobs/foo.yaml)",
    )
    parser.add_argument(
        "--model-config", type=Path, required=True,
        help="Path to the model YAML (e.g. configs/models/bar.yaml)",
    )
    parser.add_argument(
        "--instance-id", required=True,
        help="Unique instance identifier for this run",
    )
    parser.add_argument(
        "--instance-dir", type=Path, required=True,
        help="Directory to write manifest.json into",
    )
    parser.add_argument(
        "--force-proxy", action="store_true",
        help="Record FORCE_PROXY=1 as a local-proxy requirement; mode stays explicit",
    )
    parser.add_argument(
        "--harness-backend", default=None,
        help="Harness execution backend (local) used for mount selection",
    )
    parser.add_argument(
        "--no-stage", action="store_true",
        help="Skip copying the dataset to a throwaway staging dir (dry-run: no "
             "prepare_tasks runs, so nothing would mutate the real dataset).",
    )
    args = parser.parse_args()

    from datetime import datetime, timezone

    try:
        manifest_path = resolve_and_write(
            instance_dir=args.instance_dir,
            job_config_path=args.job_config,
            model_config_path=args.model_config,
            instance_id=args.instance_id,
            force_proxy=args.force_proxy,
            harness_backend=args.harness_backend,
            stage=not args.no_stage,
            resolved_at=datetime.now(timezone.utc).isoformat(),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: manifest resolution failed: {exc}", file=sys.stderr)
        return 1

    # Output path for run.sh to capture
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
