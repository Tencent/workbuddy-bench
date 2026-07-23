#!/usr/bin/env bash
# Standalone host-side LLM judge for already-finished runs (routed through the
# proxy, which this script auto-starts).
#
# Post-hoc add judge metrics to result dirs that were run WITHOUT the judge (or
# re-judge with a different model). The judge model/endpoint/params come from the
# job config's llm_judge slug (configs/models/<slug>.yaml) — same switch + slug as
# a live run, nothing hardcoded.
#
# Transport: routed through the proxy. This script auto-starts the shared proxy
# (scripts/proxy/proxy-shared.sh start, idempotent), registers the judge slug route
# into it (proxy_config --judge-only + POST /admin/reload), then points the judge
# at the proxy. The proxy injects the judge model's extra_body (e.g.
# reasoning_effort) and holds the upstream key.
#
# Usage:
#   scripts/judge/run-judge.sh --job-config configs/jobs/<job>.yaml \
#       --jobs results/<dir> [results/<dir2> ...] [--no-write-back]
#
# The job config supplies the judge slug (llm_judge_override or bench default with
# enabled: true). mode: host_side only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# Load backend credentials/URLs (the judge slug's backend_url_env / backend_key_env).
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
fi

cd "$REPO_ROOT"
if [ -n "${PYTHON:-}" ]; then
    PYTHON_CMD=("$PYTHON")
else
    PYTHON_CMD=(uv run python)
fi

# ── Parse the args we care about; leave the rest for run_post_judge ──────────
JOB_CONFIG=""
PASS_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --job-config) JOB_CONFIG="$2"; PASS_ARGS+=("$1" "$2"); shift 2;;
        *)            PASS_ARGS+=("$1"); shift;;
    esac
done

if [ -z "$JOB_CONFIG" ]; then
    echo "ERROR: --job-config is required." >&2
    exit 1
fi

# ── Resolve the judge slug (run_post_judge owns the resolution; no shell dup) ─
read -r JUDGE_SLUG JUDGE_ENABLED < <(
    "${PYTHON_CMD[@]}" -m workbuddy_bench.runner.run_post_judge \
        --print-judge-slug --job-config "$JOB_CONFIG"
)

if [ "$JUDGE_ENABLED" != "1" ] || [ "$JUDGE_SLUG" = "-" ] || [ -z "$JUDGE_SLUG" ]; then
    echo "ERROR: $JOB_CONFIG has no enabled judge slug." >&2
    echo "  Enable it: llm_judge_override: {enabled: true, model: <slug>}" >&2
    exit 1
fi

JUDGE_MODEL_CONFIG="$REPO_ROOT/configs/models/$JUDGE_SLUG.yaml"
if [ ! -f "$JUDGE_MODEL_CONFIG" ]; then
    echo "ERROR: judge model config not found: $JUDGE_MODEL_CONFIG" >&2
    exit 1
fi

# shellcheck source=scripts/proxy/proxy-env.sh
source "$SCRIPT_DIR/../proxy/proxy-env.sh"
mkdir -p "$PROXY_LOG_DIR"

# 1) Auto-start the shared proxy (idempotent — reuses a healthy one).
echo "Ensuring shared proxy on :$PROXY_PORT ..."
PROXY_PORT="$PROXY_PORT" "$SCRIPT_DIR/../proxy/proxy-shared.sh" start

# 2) Register the judge slug route into the shared config (idempotent by slug).
if ! "${PYTHON_CMD[@]}" -m workbuddy_bench.runner.proxy_config \
    --judge-only \
    --judge-slug "$JUDGE_SLUG" \
    --judge-model-config "$JUDGE_MODEL_CONFIG" \
    --shared "$SHARED_CONFIG" \
    --port "$PROXY_PORT" \
    --log-dir "$PROXY_LOG_DIR" \
    --max-concurrent "$PROXY_MAX_CONCURRENT"; then
    echo "ERROR: failed to register judge route into shared config." >&2
    echo "  If this is a slug conflict, the shared proxy already has a different" >&2
    echo "  route under '$JUDGE_SLUG'; restart it (scripts/proxy/proxy-shared.sh restart)." >&2
    exit 1
fi

# 3) Hot-reload so the proxy picks up the judge route.
if ! curl -s --max-time 5 -X POST "http://localhost:$PROXY_PORT/admin/reload" 2>/dev/null \
    | "${PYTHON_CMD[@]}" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status")=="ok" else 1)' 2>/dev/null; then
    echo "ERROR: POST /admin/reload failed on :$PROXY_PORT." >&2
    echo "  Debug: tail -n 50 $PROXY_LOG_DIR/shared-proxy.log" >&2
    exit 1
fi
echo "Registered + reloaded judge route: $JUDGE_SLUG (:$PROXY_PORT)"

# 4) Judge routed through the proxy. The judge addresses its slug route on the host proxy.
PROXY_URL="http://localhost:$PROXY_PORT"
exec "${PYTHON_CMD[@]}" -m workbuddy_bench.runner.run_post_judge \
    --proxy-url "$PROXY_URL" "${PASS_ARGS[@]}"
