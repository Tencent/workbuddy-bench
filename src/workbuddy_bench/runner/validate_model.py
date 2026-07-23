"""Validate model backend configuration before starting an evaluation run.

Called by ``scripts/run.sh`` after loading ``.env`` but before prepare_tasks,
proxy startup, Docker build, or Harbor execution. Fail-fast on missing or
misconfigured model backends so operators get a clear error message instead
of discovering the problem minutes into a run.

Usage from shell::

    # From manifest (preferred — single source of truth):
    python3 -m workbuddy_bench.runner.validate_model --manifest <manifest-path>

    # Legacy (still supported for direct invocation):
    python3 -m workbuddy_bench.runner.validate_model <model-config-path>

Exit code 0 = OK, 1 = validation failure (human-readable message on stderr).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

from workbuddy_bench.runner.config_loaders import normalize_model_protocols
from workbuddy_bench.runner.resolve_manifest import manifest_connection_mode


def validate_model_backend(model_config_path: str) -> list[str]:
    """Validate the model config at the given path.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []
    config_path = Path(model_config_path)
    model_slug = config_path.stem

    # 1. Config file must exist
    if not config_path.is_file():
        errors.append(
            f"Model config not found: {config_path}\n"
            f"  Expected: configs/models/{model_slug}.yaml"
        )
        return errors  # can't validate further

    # 2. Parse YAML
    try:
        data = yaml.safe_load(config_path.read_text())
    except Exception as exc:
        errors.append(f"Failed to parse {config_path}: {exc}")
        return errors

    if not isinstance(data, dict) or "model" not in data:
        errors.append(
            f"{config_path}: missing top-level 'model:' block"
        )
        return errors

    model = data["model"]
    if not isinstance(model, dict):
        errors.append(f"{config_path}: 'model:' must be a mapping")
        return errors

    # Wire protocols the model service speaks. ``protocols`` (list or scalar;
    # first element = primary) drives direct-mode forwarding and the checks
    # below. Validate the raw shape here (a present-but-empty/malformed value is
    # an operator error we must surface) before deferring to the shared
    # normalizer for the primary protocol, so validation never silently
    # defaults it away.
    raw_protocols = model.get("protocols")
    if raw_protocols is not None:
        candidates = [raw_protocols] if isinstance(raw_protocols, str) else raw_protocols
        if not isinstance(candidates, list) or not [p for p in candidates if p]:
            errors.append(
                f"Model '{model_slug}': 'protocols' must be a non-empty list "
                f"(e.g. [openai] or [openai, anthropic]); the first element is the "
                f"primary protocol.\n  Config: {config_path}"
            )
            return errors
    protocol = normalize_model_protocols(model)[0]

    # 3. For OpenAI primary protocol: backend_url_env must be declared
    if protocol == "openai":
        backend_url_env = model.get("backend_url_env", "")
        backend_key_env = model.get("backend_key_env", "")

        if not backend_url_env:
            errors.append(
                f"Model '{model_slug}' (protocol=openai) is missing 'backend_url_env'.\n"
                f"  Config: {config_path}\n"
                f"  Fix: add backend_url_env: \"<MODEL_FAMILY>_BASE_URL\" to the model block."
            )
            return errors

        # 4. The env var must be set and non-empty
        url_value = os.environ.get(backend_url_env, "")
        if not url_value.strip():
            errors.append(
                f"Model '{model_slug}' requires env var '{backend_url_env}' but it is not set.\n"
                f"  Config: {config_path}\n"
                f"  Fix: set {backend_url_env}=\"https://your-endpoint/v1\" in .env or shell."
            )

        # 5. If backend_key_env is declared, warn if unset (some backends
        #    don't require a key, so this is a warning not an error).
        if backend_key_env:
            key_value = os.environ.get(backend_key_env, "")
            if not key_value.strip():
                # Print warning to stderr but don't treat as fatal error.
                # Some internal backends accept empty keys.
                print(
                    f"WARNING: Model '{model_slug}' declares backend_key_env='{backend_key_env}' "
                    f"but the variable is empty. Some backends require an API key.",
                    file=sys.stderr,
                )

    # For anthropic protocol, we don't require backend_url_env/backend_key_env
    # since the harness uses ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL directly.

    return errors


def validate_from_manifest(manifest_path: str) -> list[str]:
    """Validate model backend using resolved manifest as the source of truth.

    The manifest already contains the resolved backend_url and backend_key_env,
    so we validate directly without re-parsing model YAML.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []
    mpath = Path(manifest_path)

    if not mpath.is_file():
        errors.append(f"Manifest not found: {manifest_path}")
        return errors

    try:
        manifest = json.loads(mpath.read_text())
    except Exception as exc:
        errors.append(f"Failed to parse manifest: {exc}")
        return errors

    model_slug = manifest.get("model_slug", "<unknown>")
    model_protocol = (manifest.get("model_protocols") or ["openai"])[0]
    connection_mode = manifest_connection_mode(manifest)
    backend_url = manifest.get("backend_url", "")
    backend_key_env = manifest.get("backend_key_env", "")

    requires_backend_url = (
        model_protocol == "openai"
        or connection_mode == "local_proxy"
    )

    if requires_backend_url and not backend_url.strip():
        reasons: list[str] = []
        if model_protocol == "openai":
            reasons.append("protocol=openai")
        if connection_mode == "local_proxy":
            reasons.append("model_connection=local_proxy")
        reason_text = ", ".join(reasons) or "backend URL required"
        errors.append(
            f"Model '{model_slug}' ({reason_text}) has no backend URL resolved.\n"
            f"  The env var for the backend URL is not set or empty.\n"
            f"  Fix: set the appropriate *_BASE_URL variable in .env or shell."
        )

    if backend_key_env:
        key_value = os.environ.get(backend_key_env, "")
        if not key_value.strip():
            print(
                f"WARNING: Model '{model_slug}' declares backend_key_env='{backend_key_env}' "
                f"but the variable is empty. Some backends require an API key.",
                file=sys.stderr,
            )

    return errors


def main() -> int:
    # Support two modes:
    #   --manifest <path>    (preferred, reads from resolved manifest)
    #   <model-config-path>  (legacy, reads from model YAML directly)
    if len(sys.argv) >= 3 and sys.argv[1] == "--manifest":
        manifest_path = sys.argv[2]
        errors = validate_from_manifest(manifest_path)
    elif len(sys.argv) >= 2 and sys.argv[1] != "--manifest":
        model_config_path = sys.argv[1]
        errors = validate_model_backend(model_config_path)
    else:
        print(
            "Usage: python3 -m workbuddy_bench.runner.validate_model "
            "[--manifest <manifest-path> | <model-config-path>]",
            file=sys.stderr,
        )
        return 1

    if errors:
        print("", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("MODEL BACKEND VALIDATION FAILED", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        for err in errors:
            print(f"\n  {err}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "The run cannot proceed without a valid model backend. "
            "Fix the above and retry.",
            file=sys.stderr,
        )
        print("=" * 60, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
