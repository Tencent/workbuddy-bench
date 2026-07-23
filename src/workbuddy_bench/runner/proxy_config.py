"""Generate route-scoped proxy configs for WorkBuddy Bench runs.

The runtime manifest is the source of truth for the route that an agent will
request.  This module turns that single route into a proxy YAML file consumable
by ``proxy/main.py --config`` so local-proxy jobs do not need to scan every model
under ``configs/models``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from workbuddy_bench.runner.config_loaders import load_yaml
from workbuddy_bench.runner.model_params import flatten_params
from workbuddy_bench.runner.resolve_manifest import manifest_connection_mode


def resolve_proxy_route(*, harness_protocol: str, model_protocols: list[str]) -> dict[str, str]:
    """Return proxy mode plus explicit client/backend protocol metadata."""
    if harness_protocol not in {"anthropic", "openai"}:
        raise ValueError(
            f"local_proxy does not support harness protocol {harness_protocol!r}; "
            "implemented harness protocols are 'anthropic' and 'openai'."
        )

    # Prefer same-protocol passthrough when the backend supports the harness wire
    # protocol. Conversion modes are only for genuinely cross-protocol pairs.
    if harness_protocol in model_protocols:
        return {
            "mode": "passthrough",
            "client_protocol": harness_protocol,
            "backend_protocol": harness_protocol,
        }

    if harness_protocol == "anthropic" and "openai" in model_protocols:
        return {
            "mode": "a2o",
            "client_protocol": "anthropic",
            "backend_protocol": "openai",
        }

    if harness_protocol == "openai" and "anthropic" in model_protocols:
        raise ValueError(
            "local_proxy cannot bridge OpenAI harness -> Anthropic backend yet "
            "(O2A is a planned proxy mode but is not implemented)."
        )

    raise ValueError(
        "local_proxy cannot select an implemented upstream protocol from "
        f"model_protocols={model_protocols!r}; supported pairs are same-protocol "
        "passthrough and Anthropic harness -> OpenAI backend (a2o)."
    )


def resolve_proxy_mode(*, harness_protocol: str, model_protocols: list[str]) -> str:
    """Return the implemented proxy mode for one harness/backend protocol pair."""
    return resolve_proxy_route(
        harness_protocol=harness_protocol,
        model_protocols=model_protocols,
    )["mode"]


# Connection modes that put a bench proxy in the request path. ``local_proxy``
# runs a job-private proxy on the host, reachable by docker containers via
# host.docker.internal. It is the only proxy mode currently supported.
_PROXY_CONNECTION_MODES = ("local_proxy",)


def build_proxy_config(
    *,
    manifest: dict[str, Any],
    model_config: dict[str, Any],
    port: int,
    log_dir: str,
    max_concurrent: int,
    backend_timeout: float,
    backend_retries: int,
    retry_base_delay: float,
    default_experiment: str,
    default_harness: str,
    host: str = "0.0.0.0",
) -> dict[str, Any]:
    """Build a proxy YAML mapping with exactly one route from a manifest.

    ``host`` controls the proxy bind address. The host proxy binds ``0.0.0.0``
    so docker containers can reach it via host.docker.internal.
    """
    connection_mode = manifest_connection_mode(manifest)
    if connection_mode not in _PROXY_CONNECTION_MODES:
        raise ValueError(
            "proxy config can only be generated for model_connection in "
            f"{_PROXY_CONNECTION_MODES}; got {connection_mode!r}"
        )

    model = model_config.get("model")
    if not isinstance(model, dict):
        raise ValueError("model config is missing a top-level 'model:' mapping")

    connection = manifest.get("connection") or {}
    backend_url_env = (
        connection.get("upstream_backend_url_env")
        or model.get("backend_url_env")
        or ""
    )
    backend_key_env = (
        connection.get("upstream_backend_key_env")
        or model.get("backend_key_env")
        or ""
    )
    if not backend_url_env:
        raise ValueError(
            "local_proxy route requires model.backend_url_env so the job-private proxy "
            "can reach the upstream model backend."
        )

    model_route = manifest.get("model_route") or manifest.get("model_slug")
    backend_model_name = manifest.get("backend_model_name") or model.get("name") or model_route
    if not model_route:
        raise ValueError("manifest is missing model_route/model_slug")
    if not backend_model_name:
        raise ValueError("manifest/model config is missing backend model name")

    harness_protocol = str(manifest.get("harness_protocol") or "openai")
    model_protocols = manifest.get("model_protocols")
    if not model_protocols:
        raise ValueError("manifest is missing model_protocols")
    route_protocol = resolve_proxy_route(
        harness_protocol=harness_protocol,
        model_protocols=model_protocols,
    )
    mode = route_protocol["mode"]

    extra_body = (manifest.get("request_overrides") or {}).get("extra_body") or {}
    if not isinstance(extra_body, dict):
        raise ValueError("manifest request_overrides.extra_body must be a mapping when present")

    interceptors = ["log"]
    if extra_body and route_protocol["backend_protocol"] != "anthropic":
        interceptors.append("inject_extra_body")

    # Per-trial id, carried on the route so the proxy can tag/split logs by trial.
    # Only meaningful when record_full_io made the route instance-specific.
    instance_id = str(manifest.get("instance_id") or "") if manifest.get("record_full_io") else ""

    # Optional per-model concurrency cap (model.max_concurrent in configs/models).
    # Absent => unlimited: the proxy adds no cap for this model (concurrency is
    # bounded upstream by the eval job's container/trial limits).
    backend_block: dict[str, Any] = {
        "url_env": str(backend_url_env),
        "key_env": str(backend_key_env),
    }
    model_max_concurrent = model.get("max_concurrent")
    if model_max_concurrent is not None:
        backend_block["max_concurrent"] = int(model_max_concurrent)
    # Optional custom headers injected into every upstream request (literal
    # values, stringified to guard against YAML int/bool). Only meaningful under
    # local_proxy. Reserved headers (host/content-length/authorization/x-api-key)
    # are rejected here so an invalid header fails fast at config generation
    # rather than being silently dropped by the sender at request time (which
    # would leave a never-honored header in the generated proxy.yaml).
    model_headers = model.get("backend_headers")
    if isinstance(model_headers, dict) and model_headers:
        _reserved = {"host", "content-length", "authorization", "x-api-key"}
        bad = sorted(k for k in model_headers if str(k).lower() in _reserved)
        if bad:
            raise ValueError(
                f"model.backend_headers may not set reserved headers {bad}; "
                "the backend key's Authorization is injected from backend_key_env "
                "and host/content-length are transport framing."
            )
        backend_block["headers"] = {
            str(k): str(v) for k, v in model_headers.items()
        }

    routes = [
        {
            "slug": str(model_route),
            "mode": mode,
            "backend": backend_block,
            "backend_model": str(backend_model_name),
            "client_protocol": route_protocol["client_protocol"],
            "backend_protocol": route_protocol["backend_protocol"],
            "extra_body": extra_body,
            "interceptors": interceptors,
            "instance_id": instance_id,
            # Backend-quirk workarounds, opt-in per model (default off) for
            # backends that duplicate thinking across reasoning keys.
            "dedup_reasoning": bool(model.get("dedup_reasoning", False)),
            "reasoning_passback": bool(model.get("reasoning_passback", False)),
        }
    ]

    # Add a second route for the LLM judge so host-side and in-container judges
    # can talk to their own model slug through the SAME proxy. The judge model
    # differs from the eval model; resolve_manifest stored the judge endpoint
    # env + slug in manifest['llm_judge']. The judge speaks OpenAI to the proxy
    # (passthrough); its model.params.extra_body is injected here so the caller
    # does not hardcode model-specific body knobs. Only added when the judge will
    # run (enabled).
    judge_route = _judge_route(manifest)
    if judge_route and judge_route["slug"] != str(model_route):
        routes.append(judge_route)

    return {
        "proxy": {
            "host": str(host),
            "port": int(port),
            "log_dir": str(log_dir),
            # Full request/response logging is opt-in per job via record_full_io
            # (default off → no large shared log file). When on, the logger splits
            # output per trial using each route's instance_id.
            "log_enabled": bool(manifest.get("record_full_io", False)),
            "max_concurrent": int(max_concurrent),
            "backend_timeout": float(backend_timeout),
            "backend_retries": int(backend_retries),
            "retry_base_delay": float(retry_base_delay),
            "default_experiment": str(default_experiment),
            "default_harness": str(default_harness),
            "routes": routes,
        }
    }


def _openai_judge_route(
    *, slug: str, url_env: str, key_env: str, backend_model: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Assemble an OpenAI-passthrough judge route from resolved parts.

    Shared by the live-run (``_judge_route``) and post-hoc
    (``_judge_route_from_model_config``) builders so the route shape stays in
    lockstep. The YAML ``extra_body`` key intentionally carries the model's full
    flattened request params, including top-level sampling knobs and nested
    ``params.extra_body`` entries.
    """
    injected = flatten_params(params)
    interceptors = ["log"]
    if injected:
        interceptors.append("inject_extra_body")
    return {
        "slug": slug,
        "mode": "passthrough",
        "backend": {"url_env": url_env, "key_env": key_env},
        "backend_model": backend_model,
        "client_protocol": "openai",
        "backend_protocol": "openai",
        "extra_body": injected,
        "interceptors": interceptors,
    }


def _judge_route(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Build an OpenAI-passthrough proxy route for the configured LLM judge.

    Returns None unless ``manifest['llm_judge']`` is enabled and fully resolved
    (model_slug + api_base_env). The judge model's params are injected by the
    proxy via the inject_extra_body interceptor.
    """
    judge = manifest.get("llm_judge") or {}
    if not judge.get("enabled"):
        return None
    slug = str(judge.get("model_slug") or "")
    url_env = str(judge.get("api_base_env") or "")
    key_env = str(judge.get("api_key_env") or "")
    backend_model = str(judge.get("model") or "")
    if not slug or not url_env or not backend_model:
        return None

    params = judge.get("params") if isinstance(judge.get("params"), dict) else {}
    return _openai_judge_route(
        slug=slug, url_env=url_env, key_env=key_env,
        backend_model=backend_model, params=params,
    )


def write_proxy_config(
    *,
    manifest_path: Path,
    model_config_path: Path,
    output_path: Path,
    port: int,
    log_dir: str,
    max_concurrent: int,
    backend_timeout: float,
    backend_retries: int,
    retry_base_delay: float,
    default_experiment: str,
    default_harness: str,
) -> Path:
    manifest = json.loads(manifest_path.read_text())
    model_config = load_yaml(model_config_path)
    proxy_config = build_proxy_config(
        manifest=manifest,
        model_config=model_config,
        port=port,
        log_dir=log_dir,
        max_concurrent=max_concurrent,
        backend_timeout=backend_timeout,
        backend_retries=backend_retries,
        retry_base_delay=retry_base_delay,
        default_experiment=default_experiment,
        default_harness=default_harness,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(proxy_config, sort_keys=False, allow_unicode=True))
    return output_path


class RouteConflictError(ValueError):
    """Raised when merging a route whose slug already exists with different content.

    The shared proxy keys routes by slug. Re-adding an identical route is an
    idempotent no-op, but a same-slug route with a different backend / extra_body
    / mode would silently shadow the existing one — so we refuse and ask the
    operator to use a distinct model-config slug instead.
    """

    def __init__(self, slug: str):
        super().__init__(
            f"route slug {slug!r} already loaded in the shared proxy with different "
            "content (backend/extra_body/mode). Refusing to overwrite. To run the "
            "same model with different params, create a new configs/models/<slug>.yaml."
        )
        self.slug = slug


def _judge_route_from_model_config(
    *, slug: str, model_config: dict[str, Any]
) -> dict[str, Any]:
    """Build an OpenAI-passthrough judge route directly from a judge model config.

    Post-hoc judging has no eval manifest (the run is over), so we can't go
    through ``build_proxy_config`` (which requires an eval model route). Instead
    we construct the judge's own route straight from ``configs/models/<slug>.yaml``
    — the same shape ``_judge_route`` produces for a live run: passthrough,
    OpenAI↔OpenAI, extra_body injected by the proxy.
    """
    model = model_config.get("model")
    if not isinstance(model, dict):
        raise ValueError(f"judge model config for slug {slug!r} is missing a 'model:' block")
    url_env = str(model.get("backend_url_env") or "")
    key_env = str(model.get("backend_key_env") or "")
    backend_model = str(model.get("name") or "")
    if not url_env or not backend_model:
        raise ValueError(
            f"judge model config for slug {slug!r} must define backend_url_env and name"
        )
    params = model.get("params") if isinstance(model.get("params"), dict) else {}
    return _openai_judge_route(
        slug=slug, url_env=url_env, key_env=key_env,
        backend_model=backend_model, params=params,
    )


def merge_judge_route_into_shared_config(
    *,
    judge_slug: str,
    judge_model_config_path: Path,
    shared_path: Path,
    port: int,
    log_dir: str,
    max_concurrent: int,
) -> tuple[Path, str]:
    """Merge ONLY a judge slug route into the shared proxy config, by slug.

    Used by post-hoc judging (via proxy) where there is no eval model to register
    — just the judge. Same by-slug merge semantics + flock guard as
    ``merge_route_into_shared_config``. Returns ``(shared_path, judge_slug)``.
    """
    model_config = load_yaml(judge_model_config_path)
    route = _judge_route_from_model_config(slug=judge_slug, model_config=model_config)

    shared_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = shared_path.with_suffix(".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if shared_path.exists() and shared_path.stat().st_size > 0:
                existing = load_yaml(shared_path)
            else:
                existing = {"proxy": {
                    "host": "0.0.0.0", "port": port, "log_dir": log_dir,
                    "log_enabled": False, "max_concurrent": max_concurrent,
                    "shared": True, "routes": [],
                }}
            proxy_block = existing.setdefault("proxy", {})
            by_slug = {r["slug"]: r for r in proxy_block.get("routes", [])}
            if judge_slug not in by_slug:
                by_slug[judge_slug] = route
            elif by_slug[judge_slug] != route:
                raise RouteConflictError(judge_slug)
            proxy_block["routes"] = list(by_slug.values())
            shared_path.write_text(
                yaml.safe_dump(existing, sort_keys=False, allow_unicode=True)
            )
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    return shared_path, judge_slug


def merge_route_into_shared_config(
    *,
    manifest_path: Path,
    model_config_path: Path,
    shared_path: Path,
    port: int,
    log_dir: str,
    max_concurrent: int,
    backend_timeout: float,
    backend_retries: int,
    retry_base_delay: float,
    default_experiment: str,
    default_harness: str,
) -> tuple[Path, list[str]]:
    """Merge this job's route(s) into the SHARED proxy config file, by slug.

    Reuses ``build_proxy_config`` to produce this job's eval (+ optional judge)
    route, then merges into ``shared_path``'s ``proxy.routes`` keyed by slug:

    * slug absent           -> appended
    * slug present, equal    -> idempotent no-op
    * slug present, differs  -> ``RouteConflictError``

    Existing top-level proxy fields (host/port/max_concurrent/...) are preserved
    — the running shared proxy owns those; this job only contributes routes. The
    read-modify-write is guarded by an advisory ``flock`` so concurrent run.sh
    invocations can't lose an append.

    Returns ``(shared_path, [slugs_contributed_by_this_job])``.
    """
    manifest = json.loads(manifest_path.read_text())
    model_config = load_yaml(model_config_path)
    fresh = build_proxy_config(
        manifest=manifest,
        model_config=model_config,
        port=port,
        log_dir=log_dir,
        max_concurrent=max_concurrent,
        backend_timeout=backend_timeout,
        backend_retries=backend_retries,
        retry_base_delay=retry_base_delay,
        default_experiment=default_experiment,
        default_harness=default_harness,
    )
    new_routes = fresh["proxy"]["routes"]

    shared_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = shared_path.with_suffix(".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if shared_path.exists() and shared_path.stat().st_size > 0:
                existing = load_yaml(shared_path)
            else:
                # Seed from this job's full config (carries top-level proxy fields).
                existing = {"proxy": dict(fresh["proxy"], routes=[])}
            proxy_block = existing.setdefault("proxy", {})
            by_slug = {r["slug"]: r for r in proxy_block.get("routes", [])}

            contributed: list[str] = []
            for route in new_routes:
                slug = route["slug"]
                contributed.append(slug)
                if slug not in by_slug:
                    by_slug[slug] = route
                elif by_slug[slug] != route:
                    raise RouteConflictError(slug)
                # else: identical -> idempotent no-op

            proxy_block["routes"] = list(by_slug.values())
            shared_path.write_text(
                yaml.safe_dump(existing, sort_keys=False, allow_unicode=True)
            )
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    return shared_path, contributed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a job-private proxy YAML from a resolved manifest."
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model-config", type=Path)
    # Judge-only mode (post-hoc via proxy): register just a judge slug route into a
    # shared config, with no eval manifest (the run is over). Requires --shared.
    parser.add_argument("--judge-only", action="store_true",
                        help="Register only a judge slug route (no eval manifest). Needs --shared.")
    parser.add_argument("--judge-slug", default="",
                        help="Judge model slug (route key) for --judge-only.")
    parser.add_argument("--judge-model-config", type=Path,
                        help="configs/models/<judge-slug>.yaml for --judge-only.")
    # --output: write a single-route job-private config (default mode).
    # --shared: merge this job's route(s) into a shared multi-route config file.
    # Exactly one of the two is required.
    out_group = parser.add_mutually_exclusive_group(required=True)
    out_group.add_argument("--output", type=Path, help="Write a job-private single-route config here.")
    out_group.add_argument("--shared", type=Path, help="Merge route(s) into this shared multi-route config.")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log-dir", required=True)
    # Retry defaults read the same env as the proxy (WBBENCH_PROXY_MAX_RETRIES /
    # _RETRY_DELAY_MS). Fixed-interval retry: 6 * 5s = 30s window, well inside
    # cbc's 600s timeout.
    def _retries_default() -> int:
        raw = os.environ.get("WBBENCH_PROXY_MAX_RETRIES")
        if raw is not None:
            try:
                v = int(raw)
                if v >= 0:
                    return v
            except ValueError:
                pass
        return 6

    def _delay_default_s() -> float:
        raw = os.environ.get("WBBENCH_PROXY_RETRY_DELAY_MS")
        if raw is not None:
            try:
                v = float(raw)
                if v > 0:
                    return v / 1000.0
            except ValueError:
                pass
        return 5.0

    parser.add_argument("--max-concurrent", type=int, default=16)
    parser.add_argument("--backend-timeout", type=float, default=600.0)
    parser.add_argument("--backend-retries", type=int, default=_retries_default())
    parser.add_argument("--retry-base-delay", type=float, default=_delay_default_s())
    parser.add_argument("--default-experiment", default="")
    parser.add_argument("--default-harness", default="")
    args = parser.parse_args()

    try:
        if args.judge_only:
            if args.shared is None or not args.judge_slug or args.judge_model_config is None:
                parser.error("--judge-only requires --shared, --judge-slug and --judge-model-config")
            output, slug = merge_judge_route_into_shared_config(
                judge_slug=args.judge_slug,
                judge_model_config_path=args.judge_model_config,
                shared_path=args.shared,
                port=args.port,
                log_dir=args.log_dir,
                max_concurrent=args.max_concurrent,
            )
            print(f"{output} routes={slug}")
            return 0

        if args.manifest is None or args.model_config is None:
            parser.error("--manifest and --model-config are required unless --judge-only")

        if args.shared is not None:
            output, slugs = merge_route_into_shared_config(
                manifest_path=args.manifest,
                model_config_path=args.model_config,
                shared_path=args.shared,
                port=args.port,
                log_dir=args.log_dir,
                max_concurrent=args.max_concurrent,
                backend_timeout=args.backend_timeout,
                backend_retries=args.backend_retries,
                retry_base_delay=args.retry_base_delay,
                default_experiment=args.default_experiment,
                default_harness=args.default_harness,
            )
            print(f"{output} routes={','.join(slugs)}")
            return 0

        output = write_proxy_config(
            manifest_path=args.manifest,
            model_config_path=args.model_config,
            output_path=args.output,
            port=args.port,
            log_dir=args.log_dir,
            max_concurrent=args.max_concurrent,
            backend_timeout=args.backend_timeout,
            backend_retries=args.backend_retries,
            retry_base_delay=args.retry_base_delay,
            default_experiment=args.default_experiment,
            default_harness=args.default_harness,
        )
    except RouteConflictError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: proxy config generation failed: {exc}", file=sys.stderr)
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
