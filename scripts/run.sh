#!/bin/bash
# Unified evaluation runner for WorkBuddy Bench (external edition).
#
# Reads model configs from configs/models/*.yaml and job configs from
# configs/jobs/. Honors each job's explicit model_connection (direct |
# local_proxy). Loads .env for backend credentials.
#
# The external edition runs only the local Docker harness backend, with either:
#   - direct      : harness talks to the model backend directly
#   - local_proxy : a job-private host proxy (protocol conversion + extra_body
#                   injection + logging) sits between harness and backend
#
# Usage:
#   uv run ./scripts/run.sh --job <slug>            # job is the entry point
#   uv run ./scripts/run.sh --job <slug> --dry-run  # resolve manifest, print, exit
#   SHARDS=4 uv run ./scripts/run.sh --job <slug>   # sharded / task_selection
#
# The model and dataset come from the job YAML's ``model:`` / ``dataset:`` keys
# (single source of truth). To run a different model/dataset, edit/add a job.
set -e

# ── Parse arguments ──────────────────────────────────────────────
JOB_SLUG=""
DRY_RUN="${DRY_RUN:-0}"  # honor `DRY_RUN=1 ./run.sh ...`; --dry-run also sets it
while [[ $# -gt 0 ]]; do
    case $1 in
        --job)     JOB_SLUG="$2"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        --help|-h)
            echo "Usage: $0 --job <slug> [--dry-run]"
            echo ""
            echo "Jobs (from configs/jobs/, excluding _template*):"
            ls configs/jobs/*.yaml 2>/dev/null \
                | xargs -I{} basename {} .yaml \
                | grep -v '^_' | sed 's/^/  /'
            echo ""
            echo "Environment overrides:"
            echo "  SHARDS=N              Parallel harbor shards (default: 1)"
            echo "  SHARD_CONCURRENCY=N   Concurrency within each shard (default: job n_concurrent_trials, else 2)"
            echo "  NO_FORCE_BUILD=1      Skip Docker image rebuild"
            echo "  DISABLE_VERIFICATION=1  Run agent rollout only; skip task verification and host-side LLM judge"
            echo "  AUTO_BUILD_HARNESS_MOUNT=1   Build a missing local harness mount image"
            echo "  PROXY_PORT=N          Preferred job-private proxy port (default: 3456)"
            echo "  SHARED_PROXY=1        Use one shared long-lived proxy on :3456 instead of a"
            echo "                        per-job one. Start it first: scripts/proxy/proxy-shared.sh start"
            exit 0;;
        *) echo "Unknown argument: $1"; exit 1;;
    esac
done

if [ -z "$JOB_SLUG" ]; then
    echo "ERROR: --job is required. Available jobs:"
    ls configs/jobs/*.yaml 2>/dev/null \
        | xargs -I{} basename {} .yaml | grep -v '^_' | sed 's/^/  /'
    exit 1
fi

# Validate numeric env knobs early. A non-numeric SHARDS (e.g. a typo "4x")
# otherwise makes `[ "$SHARDS" -gt 1 ]` emit a cryptic "integer expression
# expected" and — since it's an if-condition — set -e does not abort, so the run
# silently falls through to the unsharded path instead of failing.
if [ -n "${SHARDS:-}" ]; then
    case "$SHARDS" in
        ''|*[!0-9]*) echo "ERROR: SHARDS must be a positive integer (got '$SHARDS')."; exit 1;;
        0)           echo "ERROR: SHARDS must be >= 1 (got 0)."; exit 1;;
    esac
fi
if [ -n "${SHARD_CONCURRENCY:-}" ]; then
    case "$SHARD_CONCURRENCY" in
        ''|*[!0-9]*) echo "ERROR: SHARD_CONCURRENCY must be a positive integer (got '$SHARD_CONCURRENCY')."; exit 1;;
        0)           echo "ERROR: SHARD_CONCURRENCY must be >= 1 (got 0)."; exit 1;;
    esac
fi

# ── Resolve paths ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
REPO_ROOT="$(pwd)"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# ── Load .env if present ─────────────────────────────────────────
if [ -f "$REPO_ROOT/.env" ]; then
    set -a; source "$REPO_ROOT/.env"; set +a
    echo "Loaded environment from .env"
fi

JOB_CONFIG="configs/jobs/${JOB_SLUG}.yaml"
[ -f "$JOB_CONFIG" ] || { echo "ERROR: Job config not found: $JOB_CONFIG"; exit 1; }

MODEL_SLUG="$(python3 -c 'import sys,yaml;print((yaml.safe_load(open(sys.argv[1])) or {}).get("model",""))' "$JOB_CONFIG")"
[ -n "$MODEL_SLUG" ] || { echo "ERROR: $JOB_CONFIG has no top-level 'model:' key."; exit 1; }
MODEL_CONFIG="configs/models/${MODEL_SLUG}.yaml"
[ -f "$MODEL_CONFIG" ] || { echo "ERROR: Model config not found: $MODEL_CONFIG (referenced by $JOB_SLUG)"; exit 1; }
echo "Model: $MODEL_SLUG (from job.model)"

# ── Read model + harness adapter metadata ────────────────────────
_CONFIG_VARS="$(python3 -c "
import yaml, sys, shlex
from pathlib import Path
from workbuddy_bench.runner.config_loaders import load_harness_config

model_cfg = yaml.safe_load(open(sys.argv[1]))['model']
job_cfg   = yaml.safe_load(open(sys.argv[2]))
repo_root = Path(sys.argv[3])

def emit(name, value):
    print(f'{name}={shlex.quote(str(value))}')

emit('MODEL_NAME', model_cfg.get('name',''))
primary = model_cfg.get('protocol','openai')
protos = model_cfg.get('protocols') or [primary]
if isinstance(protos, str): protos=[protos]
protos=[str(p) for p in protos if p]
if primary not in protos: protos.insert(0, primary)
emit('MODEL_PROTOCOL', primary)
emit('MODEL_PROTOCOLS', ' '.join(protos))

harness_slug = job_cfg.get('harness')
if not harness_slug: sys.exit('ERROR: job YAML missing top-level \"harness:\"')
try:
    harness_cfg = load_harness_config(str(harness_slug), repo_root / 'configs').harness
except Exception as exc:
    sys.exit(f'ERROR: failed to load harness {harness_slug!r}: {exc}')
emit('AGENT_NAME', harness_cfg.get('name',''))

job_dataset = job_cfg.get('dataset','')
if not job_dataset: sys.exit(f'ERROR: job YAML {sys.argv[2]} missing required \"dataset:\"')
emit('JOB_DATASET', job_dataset)

model_extra_body = (model_cfg.get('params') or {}).get('extra_body')
job_extra_body = (job_cfg.get('model_params_override') or {}).get('extra_body')
emit('HAS_EXTRA_BODY', '1' if (model_extra_body or job_extra_body) else '')
emit('BACKEND_URL_ENV', model_cfg.get('backend_url_env',''))
emit('BACKEND_KEY_ENV', model_cfg.get('backend_key_env',''))
" "$MODEL_CONFIG" "$JOB_CONFIG" "$REPO_ROOT")" || exit $?
eval "$_CONFIG_VARS"

# Harness runtime adapter (env-var names: CBC_BASE_URL / CBC_PROXY_URL / etc.)
# Capture-then-eval (like the other emitters) so an unknown harness — which exits
# non-zero with only a stderr message and empty stdout — fails the run fast.
# `eval "$(failing-cmd)"` does NOT trip set -e, so without this the run would
# continue with every HARNESS_* var unset and silently misconfigure the harness.
_HARNESS_VARS="$(python3 -m workbuddy_bench.runner.harness_adapters "$AGENT_NAME")" || exit $?
eval "$_HARNESS_VARS"

# ── Resolve model connection (direct | local_proxy) ──────────────
MODEL_CONNECTION="$(python3 -c '
import sys, yaml
job = yaml.safe_load(open(sys.argv[1])) or {}
v = job.get("model_connection","local_proxy") or "local_proxy"
valid = {"direct","local_proxy"}
if v not in valid: sys.exit(f"ERROR: {sys.argv[1]}: model_connection must be one of {sorted(valid)} (got {v!r})")
print(v)
' "$JOB_CONFIG")" || exit $?

# Protocol bridge needed when harness protocol is not among the model's protocols.
NEEDS_PROXY_PROTOCOL="1"
for p in $MODEL_PROTOCOLS; do [ "$p" = "$HARNESS_PROTOCOL" ] && NEEDS_PROXY_PROTOCOL=""; done

USE_LOCAL_PROXY=""
[ "$MODEL_CONNECTION" = "local_proxy" ] && USE_LOCAL_PROXY="1"

# direct mode must not silently swallow proxy-only requirements.
if [ -z "$USE_LOCAL_PROXY" ]; then
    if [ -n "$NEEDS_PROXY_PROTOCOL" ] || [ -n "$HAS_EXTRA_BODY" ]; then
        echo "ERROR: model_connection=direct is incompatible with this job." >&2
        [ -n "$NEEDS_PROXY_PROTOCOL" ] && echo "  - protocol bridge required: harness=$HARNESS_PROTOCOL, model protocols=$MODEL_PROTOCOLS" >&2
        [ -n "$HAS_EXTRA_BODY" ] && echo "  - extra_body injection requires model_connection: local_proxy" >&2
        echo "  Fix: set model_connection: local_proxy in $JOB_CONFIG." >&2
        exit 1
    fi
fi

echo "Dataset: $JOB_DATASET"

# Datasets are downloaded from HuggingFace, not tracked in git. Fail early with a
# fetch hint instead of a confusing "no tasks" further in.
if [ ! -d "$REPO_ROOT/$JOB_DATASET" ]; then
    echo "ERROR: dataset dir not found: $JOB_DATASET" >&2
    _sub="$(printf '%s' "$JOB_DATASET" | sed -n 's#.*wb-bench-\([a-z]*\)-v.*#\1#p')"
    echo "  Datasets are downloaded from HuggingFace, not tracked in git." >&2
    echo "  Run: ./scripts/dataset/fetch-dataset.sh ${_sub:-<subset>}   (see datasets/README.md)" >&2
    exit 1
fi

# ── Per-instance state ───────────────────────────────────────────
INSTANCE_ID="${INSTANCE_ID:-${JOB_SLUG}-$$-$(date +%s)}"
INSTANCE_STATE_DIR="$REPO_ROOT/scripts/logs/instances/$INSTANCE_ID"
mkdir -p "$INSTANCE_STATE_DIR"
echo "Instance ID: $INSTANCE_ID"

# shellcheck source=scripts/proxy/proxy-env.sh
source "$SCRIPT_DIR/proxy/proxy-env.sh"
PROXY_PID=""
CLEANUP_DONE=0

# Stop only the proxy THIS run started; verify the captured PID still runs our
# job-private config before killing it. Never kill by port or broad pattern.
cleanup_instance() {
    local rc="${1:-$?}"
    [ "${CLEANUP_DONE:-0}" = "1" ] && return "$rc"
    CLEANUP_DONE=1; set +e
    if [ -n "${PROXY_PID:-}" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
        local cmd; cmd="$(ps -p "$PROXY_PID" -o args= 2>/dev/null || true)"
        if printf '%s' "$cmd" | grep -F -- "--config ${PROXY_CONFIG:-__none__}" >/dev/null 2>&1; then
            echo "Cleanup: stopping job-private proxy PID=$PROXY_PID"
            kill "$PROXY_PID" 2>/dev/null || true
        fi
    fi
    # Remove this run's throwaway dataset copy (resolve_manifest staged it under
    # .workspace/tmp/staged/<instance-id>; see _stage_dataset). Scoped to this
    # instance id so concurrent runs don't clobber each other.
    if [ -n "${INSTANCE_ID:-}" ]; then
        rm -rf "${REPO_ROOT:-.}/.workspace/tmp/staged/$INSTANCE_ID" 2>/dev/null || true
    fi
    return "$rc"
}
trap 'rc=$?; trap - EXIT; cleanup_instance "$rc"; exit "$rc"' EXIT
trap 'trap - INT TERM; cleanup_instance 130; exit 130' INT
trap 'trap - INT TERM; cleanup_instance 143; exit 143' TERM

find_free_proxy_port() {
    local port="$1"
    while fuser "$port/tcp" >/dev/null 2>&1; do port=$((port+1)); done
    echo "$port"
}

# ── Resolve manifest (single source of truth) ────────────────────
# Dry-run never runs prepare_tasks, so nothing would mutate the real dataset:
# skip staging (--no-stage) to avoid a needless dataset copy + leftover dir.
MANIFEST_PATH="$INSTANCE_STATE_DIR/manifest.json"
RESOLVE_STAGE_FLAG=()
[ "$DRY_RUN" = "1" ] && RESOLVE_STAGE_FLAG=(--no-stage)
python3 -m workbuddy_bench.runner.resolve_manifest \
    --job-config "$JOB_CONFIG" \
    --model-config "$MODEL_CONFIG" \
    --instance-id "$INSTANCE_ID" \
    --instance-dir "$INSTANCE_STATE_DIR" \
    --harness-backend local \
    "${RESOLVE_STAGE_FLAG[@]}" \
    > /dev/null
echo "Manifest: $MANIFEST_PATH"

# ── Harness mount preflight (local split-mount) ──────────────────
harness_mount_preflight() {
    local dry="$1"
    local _vars
    _vars="$(python3 - "$MANIFEST_PATH" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
hm = m.get("harness_mount") or {}
print(f'HM_REQUIRED={"1" if hm.get("required") else ""!r}')
print(f'HM_IMAGE={(hm.get("selected_image") or "")!r}')
print(f'HM_SLUG={(m.get("harness_resolved_slug") or m.get("harness_slug") or "")!r}')
PY
    )" || exit $?
    eval "$_vars"
    [ -n "${HM_REQUIRED:-}" ] || return 0
    [ -n "${HM_IMAGE:-}" ] || { echo "ERROR: harness mount required but no image in manifest." >&2; exit 1; }
    if [ "$dry" = "1" ]; then
        echo "Dry-run: local harness mount image required: ${HM_IMAGE}"
        echo "  Build: scripts/harness/build-harness-mounts.sh --harness ${HM_SLUG}"
        return 0
    fi
    if docker image inspect "$HM_IMAGE" >/dev/null 2>&1; then
        echo "Harness mount image ready: ${HM_IMAGE}"; return 0
    fi
    if [ "${AUTO_BUILD_HARNESS_MOUNT:-0}" = "1" ]; then
        echo "Building missing harness mount image: ${HM_IMAGE}"
        scripts/harness/build-harness-mounts.sh --harness "$HM_SLUG"
        docker image inspect "$HM_IMAGE" >/dev/null 2>&1 && { echo "Harness mount image ready: ${HM_IMAGE}"; return 0; }
        echo "ERROR: build completed but image still missing: ${HM_IMAGE}" >&2; exit 1
    fi
    echo "ERROR: missing local harness mount image: ${HM_IMAGE}" >&2
    echo "  Build it: scripts/harness/build-harness-mounts.sh --harness ${HM_SLUG}" >&2
    echo "  Or: AUTO_BUILD_HARNESS_MOUNT=1 uv run ./scripts/run.sh --job ${JOB_SLUG}" >&2
    exit 1
}

# ── Dry-run: show manifest and exit ──────────────────────────────
if [ "$DRY_RUN" = "1" ]; then
    echo ""
    echo "=== Resolved Manifest (dry-run) ==="
    python3 -m json.tool "$MANIFEST_PATH"
    harness_mount_preflight "1"
    exit 0
fi

harness_mount_preflight "0"

# ── Validate model backend (fail-fast) ───────────────────────────
python3 -m workbuddy_bench.runner.validate_model --manifest "$MANIFEST_PATH"

# ── Read resolved manifest values ────────────────────────────────
_MANIFEST_VARS="$(python3 -c "
import json, sys, shlex
m = json.load(open(sys.argv[1]))
def emit(n,v): print(f'{n}={shlex.quote(str(v))}')
emit('EFFECTIVE_TASKS_DIR', m['dataset'])
emit('BENCH_MODEL_ROUTE', m.get('model_route',''))
emit('SELECTED_TASK_COUNT', len(m.get('selected_tasks') or []))
" "$MANIFEST_PATH")" || exit $?
eval "$_MANIFEST_VARS"
HAS_TASK_SELECTION=0
[ "${SELECTED_TASK_COUNT:-0}" -gt 0 ] && HAS_TASK_SELECTION=1 && echo "Task selection: $SELECTED_TASK_COUNT task(s)"

# ── Configure connection ─────────────────────────────────────────
wait_for_proxy_route() {
    local route="$1" deadline=$(( SECONDS + ${PROXY_ROUTE_WAIT:-20} ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if curl -s --max-time 2 "http://localhost:$PROXY_PORT/health" 2>/dev/null \
            | python3 -c 'import json,sys; sys.exit(0 if sys.argv[1] in json.load(sys.stdin).get("routes",{}) else 1)' "$route" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

wait_for_proxy_routes() {
    local routes_csv="$1"
    local route
    local -a routes=()
    local -a missing=()
    IFS=',' read -r -a routes <<< "$routes_csv"
    for route in "${routes[@]}"; do
        [ -z "$route" ] && continue
        if wait_for_proxy_route "$route"; then
            echo "Proxy route confirmed: $route (:$PROXY_PORT)"
        else
            missing+=("$route")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "ERROR: proxy route(s) not loaded on :$PROXY_PORT after ${PROXY_ROUTE_WAIT:-20}s: ${missing[*]}" >&2
        echo "  Debug: curl -s http://localhost:$PROXY_PORT/health | python3 -m json.tool" >&2
        return 1
    fi
}

if [ -n "$USE_LOCAL_PROXY" ] && [ "${SHARED_PROXY:-0}" = "1" ]; then
    # ── Shared long-lived proxy (SHARED_PROXY=1) ─────────────────────
    # Detect-and-reuse a single shared proxy on the fixed port; never start or
    # stop it here (use scripts/proxy/proxy-shared.sh). This run only appends its route
    # and hot-reloads. We deliberately leave PROXY_PID/PROXY_CONFIG unset, so the
    # EXIT trap's cleanup_instance is a no-op for the shared proxy (it must
    # outlive us).
    mkdir -p "$PROXY_LOG_DIR"   # PROXY_PORT fixed here; no find_free_proxy_port

    if ! curl -s --max-time 2 "http://localhost:$PROXY_PORT/health" 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status")=="ok" else 1)' 2>/dev/null; then
        echo "ERROR: SHARED_PROXY=1 but no shared proxy responding on :$PROXY_PORT." >&2
        echo "  Start it first: scripts/proxy/proxy-shared.sh start" >&2
        exit 1
    fi

    # Back-fill the manifest proxy_url (same shape as the job-private path) so
    # prepare_job points the harness at the shared proxy.
    PROXY_HOST_URL="http://$PROXY_HOST:$PROXY_PORT"
    python3 - "$MANIFEST_PATH" "$PROXY_HOST_URL" <<'PY'
import json, sys
path, proxy_url = sys.argv[1], sys.argv[2]
m = json.load(open(path))
conn = m.setdefault("connection", {})
conn["proxy_url"] = proxy_url
conn["harness_base_url"] = proxy_url
conn["proxy_location"] = "host"
conn["uses_proxy"] = True
m["proxy_url"] = proxy_url
json.dump(m, open(path, "w"), indent=2, ensure_ascii=False)
PY

    # Always merge this job's full route set. The eval route may already be
    # present while an LLM judge route is still missing, so checking only
    # BENCH_MODEL_ROUTE is not sufficient for shared-proxy reuse.
    if ! _SHARED_PROXY_CONFIG_OUT="$(python3 -m workbuddy_bench.runner.proxy_config \
        --manifest "$MANIFEST_PATH" \
        --model-config "$MODEL_CONFIG" \
        --shared "$SHARED_CONFIG" \
        --port "$PROXY_PORT" \
        --log-dir "$PROXY_LOG_DIR" \
        --max-concurrent "$PROXY_MAX_CONCURRENT" \
        --default-experiment "$JOB_SLUG" \
        --default-harness "$AGENT_NAME")"; then
        echo "ERROR: failed to merge route(s) into shared config (see message above)." >&2
        echo "  If this is a slug conflict, create a new configs/models/<slug>.yaml." >&2
        exit 1
    fi
    SHARED_PROXY_ROUTES="${_SHARED_PROXY_CONFIG_OUT##* routes=}"
    if [ -z "$SHARED_PROXY_ROUTES" ] || [ "$SHARED_PROXY_ROUTES" = "$_SHARED_PROXY_CONFIG_OUT" ]; then
        echo "ERROR: proxy_config did not report contributed routes: $_SHARED_PROXY_CONFIG_OUT" >&2
        exit 1
    fi
    if ! curl -s --max-time 5 -X POST "http://localhost:$PROXY_PORT/admin/reload" 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status")=="ok" else 1)' 2>/dev/null; then
        echo "ERROR: POST /admin/reload failed on :$PROXY_PORT." >&2
        echo "  Debug: tail -n 50 $PROXY_LOG_DIR/shared-proxy.log" >&2
        exit 1
    fi
    echo "Merged + reloaded route(s) into shared proxy: $SHARED_PROXY_ROUTES"

    # Bind harness proxy env (same as job-private).
    if [ "$HARNESS_USES_ANTHROPIC_ENV" = "1" ]; then
        export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-dummy-for-proxy}"
        export ANTHROPIC_BASE_URL="$PROXY_HOST_URL"
    elif [ -n "$HARNESS_PROXY_URL_ENV" ]; then
        export "${HARNESS_PROXY_URL_ENV}=$PROXY_HOST_URL"
    fi

    echo "=== Running WorkBuddy Bench: $MODEL_SLUG ==="
    echo "Model: $MODEL_NAME (local_proxy shared :$PROXY_PORT, routes=$SHARED_PROXY_ROUTES)"
    echo "Harness: $HARNESS_DISPLAY_NAME ($AGENT_NAME)"
    echo "Shared proxy not managed by this run; stop with: scripts/proxy/proxy-shared.sh stop"

    # Confirm every route contributed by this job, including verifier/post
    # judge routes that may differ from the eval model route.
    if [ "${SKIP_PROXY_ROUTE_CHECK:-0}" != "1" ]; then
        wait_for_proxy_routes "$SHARED_PROXY_ROUTES"
    fi
elif [ -n "$USE_LOCAL_PROXY" ]; then
    mkdir -p "$PROXY_LOG_DIR"
    if fuser "$PROXY_PORT/tcp" >/dev/null 2>&1; then
        NEW_PORT="$(find_free_proxy_port "$((PROXY_PORT+1))")"
        echo "Port :$PROXY_PORT busy; using job-private proxy on :$NEW_PORT"
        PROXY_PORT="$NEW_PORT"
    fi

    # Record the proxy URL into the manifest BEFORE prepare_job reads it, so the
    # agent block's connection.proxy_url points the harness at this proxy. The
    # harness reaches the host proxy from inside its container via
    # host.docker.internal (PROXY_HOST; override for non-Docker runtimes).
    PROXY_HOST_URL="http://$PROXY_HOST:$PROXY_PORT"
    python3 - "$MANIFEST_PATH" "$PROXY_HOST_URL" <<'PY'
import json, sys
path, proxy_url = sys.argv[1], sys.argv[2]
m = json.load(open(path))
conn = m.setdefault("connection", {})
conn["proxy_url"] = proxy_url
conn["harness_base_url"] = proxy_url
conn["proxy_location"] = "host"
conn["uses_proxy"] = True
m["proxy_url"] = proxy_url
json.dump(m, open(path, "w"), indent=2, ensure_ascii=False)
PY

    # Proxy concurrency must keep up with harbor's n_concurrent_trials. Default
    # generous (64, PROXY_MAX_CONCURRENT in proxy/proxy-env.sh); raise for high fan-out.
    PROXY_CONFIG="$INSTANCE_STATE_DIR/proxy.yaml"
    python3 -m workbuddy_bench.runner.proxy_config \
        --manifest "$MANIFEST_PATH" \
        --model-config "$MODEL_CONFIG" \
        --output "$PROXY_CONFIG" \
        --port "$PROXY_PORT" \
        --log-dir "$PROXY_LOG_DIR" \
        --max-concurrent "$PROXY_MAX_CONCURRENT" \
        --default-experiment "$JOB_SLUG" \
        --default-harness "$AGENT_NAME" \
        > /dev/null

    # Bind harness proxy env. Claude-style harnesses use ANTHROPIC_BASE_URL;
    # OpenAI-protocol harnesses (cbc) use their HARNESS_PROXY_URL_ENV (CBC_PROXY_URL).
    # PROXY_HOST_URL was set above (when the manifest was updated).
    if [ "$HARNESS_USES_ANTHROPIC_ENV" = "1" ]; then
        export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-dummy-for-proxy}"
        export ANTHROPIC_BASE_URL="$PROXY_HOST_URL"
    elif [ -n "$HARNESS_PROXY_URL_ENV" ]; then
        export "${HARNESS_PROXY_URL_ENV}=$PROXY_HOST_URL"
    fi

    echo "=== Running WorkBuddy Bench: $MODEL_SLUG ==="
    echo "Model: $MODEL_NAME (local_proxy, job-private proxy :$PROXY_PORT, route=$BENCH_MODEL_ROUTE)"
    echo "Harness: $HARNESS_DISPLAY_NAME ($AGENT_NAME)"
    echo "Proxy config: $PROXY_CONFIG"

    PROXY_STDLOG="$PROXY_LOG_DIR/proxy-$INSTANCE_ID.log"
    nohup python3 -m workbuddy_bench.proxy \
        --config "$PROXY_CONFIG" --port "$PROXY_PORT" --log-dir "$PROXY_LOG_DIR" \
        >> "$PROXY_STDLOG" 2>&1 &
    PROXY_PID=$!
    disown "$PROXY_PID" 2>/dev/null || true
    sleep 3
    echo "Job-private proxy started (PID=$PROXY_PID, log=$PROXY_STDLOG)"

    if [ "${SKIP_PROXY_ROUTE_CHECK:-0}" != "1" ] && [ -n "$BENCH_MODEL_ROUTE" ]; then
        if wait_for_proxy_route "$BENCH_MODEL_ROUTE"; then
            echo "Proxy route confirmed: $BENCH_MODEL_ROUTE (:$PROXY_PORT)"
        else
            echo "ERROR: proxy route '$BENCH_MODEL_ROUTE' not loaded on :$PROXY_PORT after ${PROXY_ROUTE_WAIT:-20}s." >&2
            echo "  Debug: curl -s http://localhost:$PROXY_PORT/health | python3 -m json.tool" >&2
            exit 1
        fi
    fi
else
    # Direct connection: the harness talks to the backend directly. For cbc the
    # agent reads CBC_BASE_URL/CBC_API_KEY; wire them from the model's env vars
    # unless already set.
    if [ -n "$HARNESS_BACKEND_BASE_ENV" ] && [ "$HARNESS_USES_ANTHROPIC_ENV" != "1" ]; then
        [ -z "${!HARNESS_BACKEND_BASE_ENV:-}" ] && [ -n "$BACKEND_URL_ENV" ] && export "${HARNESS_BACKEND_BASE_ENV}=${!BACKEND_URL_ENV:-}"
        [ -z "${!HARNESS_BACKEND_KEY_ENV:-}" ] && [ -n "$BACKEND_KEY_ENV" ] && export "${HARNESS_BACKEND_KEY_ENV}=${!BACKEND_KEY_ENV:-}"
    fi
    echo "=== Running WorkBuddy Bench: $MODEL_SLUG ==="
    echo "Model: $MODEL_NAME (direct, $MODEL_PROTOCOL protocol)"
    echo "Harness: $HARNESS_DISPLAY_NAME ($AGENT_NAME)"
fi

# ── Run evaluation ───────────────────────────────────────────────
# Resolve exec user (bench _default + per-benchmark agent_user/verifier_user,
# job-level override). Empty = leave task.toml as-is (root); prepare_tasks
# injects when non-empty. bench_config resolves configs/bench/ for the job's
# dataset so the merge matches every other reader.
_USER_VARS="$(python3 -m workbuddy_bench.runner.bench_config "$JOB_CONFIG" --emit-user-vars)" || exit $?
eval "$_USER_VARS"
prep_cmd=(python3 -m workbuddy_bench.runner.prepare_tasks "$EFFECTIVE_TASKS_DIR")
[ -n "$AGENT_USER" ] && prep_cmd+=(--agent-user "$AGENT_USER")
[ -n "$VERIFIER_USER" ] && prep_cmd+=(--verifier-user "$VERIFIER_USER")
"${prep_cmd[@]}"

if [ "${SHARDS:-1}" -gt 1 ] || [ "${HAS_TASK_SELECTION:-0}" -ne 0 ]; then
    cmd=(python3 -m workbuddy_bench.runner.sharded_eval \
        --config "$JOB_CONFIG" \
        --shards "${SHARDS:-1}" \
        --manifest "$MANIFEST_PATH")
    # Only pin per-shard concurrency when SHARD_CONCURRENCY is explicitly set;
    # otherwise sharded_eval defaults it from the job's n_concurrent_trials
    # (orchestrator_override), falling back to 2.
    [ -n "${SHARD_CONCURRENCY:-}" ] && cmd+=(--per-shard-concurrency "$SHARD_CONCURRENCY")
    [ "${NO_FORCE_BUILD:-0}" = "1" ] && cmd+=(--no-force-build)
    [ "${DISABLE_VERIFICATION:-0}" = "1" ] && cmd+=(--disable-verification)
    "${cmd[@]}"
else
    JOB_CONFIG_RUNTIME="$(python3 -m workbuddy_bench.runner.prepare_job "$JOB_CONFIG" \
        --output-dir "$REPO_ROOT/.workspace/data/generated/jobs" --manifest "$MANIFEST_PATH")"
    echo "Runtime Harbor config: $JOB_CONFIG_RUNTIME"
    harbor_cmd=(harbor run -c "$JOB_CONFIG_RUNTIME" --path "$EFFECTIVE_TASKS_DIR")
    [ "${NO_FORCE_BUILD:-0}" = "1" ] && harbor_cmd+=(--no-force-build)
    [ "${DISABLE_VERIFICATION:-0}" = "1" ] && harbor_cmd+=(--disable-verification)
    "${harbor_cmd[@]}"

    # ── Host-side LLM judge (non-sharded path) ───────────────────────
    # The sharded path runs the post-judge inside sharded_eval; the direct
    # ``harbor run`` path does not, so run it here — while the job-private proxy
    # (if any) is still alive (the EXIT trap tears it down on script exit). The
    # judge resolves its model/endpoint/params + proxy routing from the manifest
    # llm_judge block (slug-driven), so nothing is hardcoded. Disabled judges and
    # judge failures are non-fatal: the verifier reward.json is already the gate.
    if [ "${DISABLE_VERIFICATION:-0}" = "1" ]; then
        echo "=== Host-side LLM judge skipped (DISABLE_VERIFICATION=1) ==="
    elif python3 -c "
import json, sys
j = (json.load(open('$MANIFEST_PATH')).get('llm_judge') or {})
# Only the host_side mode runs as a post-run step; in_container judges run inside
# the dataset verifier during the trial.
sys.exit(0 if (j.get('enabled') and (j.get('mode') or 'host_side') == 'host_side') else 1)
"; then
        echo "=== Host-side LLM judge ==="
        python3 -m workbuddy_bench.runner.run_post_judge \
            --manifest "$MANIFEST_PATH" \
            --runtime-config "$JOB_CONFIG_RUNTIME" \
            || echo "WARNING: host-side LLM judge failed (non-fatal; verifier reward stands)."
    fi
fi

# ── Split the run-level proxy request log into per-trial files ────
# record_full_io logs every request to one run-level <instance_id>.jsonl; fan it
# out by trial_id into each results/<trial>/agent/requests.jsonl. Covers both the
# sharded and non-sharded paths (they share one instance_id). No-op unless this
# was a local_proxy run with record_full_io on. Non-fatal.
if [ "$USE_LOCAL_PROXY" = "1" ]; then
    python3 -m workbuddy_bench.runner.split_proxy_log --manifest "$MANIFEST_PATH" \
        || echo "WARNING: proxy-log split failed (non-fatal)."
fi
