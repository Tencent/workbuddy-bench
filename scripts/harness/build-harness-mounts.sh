#!/bin/bash
# Build harness split-mount images (external edition: local target only).
#
# A harness mount image is a read-only CLI layer mounted into a task/runtime
# image at /opt/<harness> (Harbor ``type: image`` volume) instead of being baked
# into every task image. The image/path contract lives in
# configs/harnesses/<family>/<version>.yaml under ``mount`` (image, path, build).
#
# Usage:
#   scripts/harness/build-harness-mounts.sh --harness codebuddy-code/2.103.4
#   scripts/harness/build-harness-mounts.sh --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."
REPO_ROOT="$(pwd)"

HARNESS_FILTER=""
DRY_RUN=0
CONFIGS_DIR="configs"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --harness) HARNESS_FILTER="$2"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        --configs-dir) CONFIGS_DIR="$2"; shift 2;;
        --help|-h)
            sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
            exit 0;;
        *) echo "Unknown argument: $1" >&2; exit 1;;
    esac
done

# Enumerate build rows. Emit TSV: slug name image dockerfile context build_args_json
mapfile -t ROWS < <(PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$REPO_ROOT" "$CONFIGS_DIR" "$HARNESS_FILTER" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from workbuddy_bench.runner.config_loaders import (
    iter_harness_configs,
    load_harness_config,
    select_harness_mount_image,
)

repo_root = Path(sys.argv[1]).resolve()
configs_dir = Path(sys.argv[2])
if not configs_dir.is_absolute():
    configs_dir = repo_root / configs_dir
filt = sys.argv[3]


def display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else repo_root / p


def dockerfile_for(mount: dict) -> Path:
    build = mount.get("build") if isinstance(mount.get("build"), dict) else {}
    configured = mount.get("dockerfile") or build.get("dockerfile")
    if configured:
        return resolve_path(str(configured))  # type: ignore[return-value]
    family = mount.get("family") or mount.get("name") or ""
    return repo_root / "configs" / "harnesses" / str(family) / "docker" / "Dockerfile"


def context_for(mount: dict, dockerfile: Path) -> Path:
    build = mount.get("build") if isinstance(mount.get("build"), dict) else {}
    configured = mount.get("context") or build.get("context")
    if configured:
        return resolve_path(str(configured))  # type: ignore[return-value]
    return dockerfile.parent


def build_args_for(harness: dict, mount: dict) -> dict[str, str]:
    build = mount.get("build") if isinstance(mount.get("build"), dict) else {}
    raw = build.get("args")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    return {}


try:
    configs = [load_harness_config(filt, configs_dir)] if filt else iter_harness_configs(configs_dir)
except Exception as exc:
    print(f"ERROR: failed to load harness config: {exc}", file=sys.stderr)
    sys.exit(1)

rows = 0
for resolved in configs:
    harness = resolved.harness
    mount = harness.get("mount") if isinstance(harness.get("mount"), dict) else {}
    if not mount:
        continue
    name = str(mount.get("name") or "")
    if not name:
        print(f"SKIP\t{resolved.slug}\tmount block missing name", file=sys.stderr)
        continue
    image = select_harness_mount_image(mount, "local")
    if not image:
        print(f"SKIP\t{resolved.slug}\tno local mount image", file=sys.stderr)
        continue
    dockerfile = dockerfile_for(mount)
    context = context_for(mount, dockerfile)
    args = build_args_for(harness, mount)
    print("\t".join([
        resolved.slug,
        name,
        image,
        display(dockerfile),
        display(context),
        json.dumps(args, sort_keys=True),
    ]))
    rows += 1

if rows == 0:
    if filt:
        print(f"No buildable mount target found for harness '{filt}'.", file=sys.stderr)
    else:
        print("No harnesses declare a buildable 'mount:' block.", file=sys.stderr)
    sys.exit(1)
PY
)

built=()
for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r slug name image dockerfile context build_args_json <<< "$row"
    if [ ! -f "$dockerfile" ]; then
        echo "WARNING: $slug ($name): $dockerfile not found; skipping." >&2
        continue
    fi
    echo "=== Building harness mount: $slug ($name) -> $image ==="
    echo "    dockerfile: $dockerfile"
    echo "    context:    $context"
    mapfile -t ARG_PAIRS < <(python3 - "$build_args_json" <<'PY'
import json, sys
for key, value in json.loads(sys.argv[1]).items():
    print(f"{key}={value}")
PY
)
    build_args=()
    for pair in "${ARG_PAIRS[@]}"; do
        build_args+=(--build-arg "$pair")
    done
    if [ "$DRY_RUN" = "1" ]; then
        echo "    dry-run: docker build -f $dockerfile ${build_args[*]} -t $image $context"
    else
        docker build -f "$dockerfile" "${build_args[@]}" -t "$image" "$context"
    fi
    built+=("$image")
done

if [ "${#built[@]}" -eq 0 ]; then
    echo "No harness mount images were built." >&2
    exit 1
fi

echo ""
echo "Built ${#built[@]} harness mount image(s):"
for img in "${built[@]}"; do echo "  $img"; done
