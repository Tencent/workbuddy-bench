#!/bin/bash
# Manage the shared, long-lived WorkBuddy Bench proxy.
#
# Unlike the per-job proxy that scripts/run.sh starts and kills, this is a single
# long-lived proxy on a fixed port (default 3456) that many jobs connect to.
# Routes accumulate as jobs run (run.sh with SHARED_PROXY=1 appends each model's
# route to the shared config and hot-reloads via POST /admin/reload).
#
# Usage:
#   scripts/proxy/proxy-shared.sh start     # start the shared proxy (idempotent)
#   scripts/proxy/proxy-shared.sh stop      # stop it (only kills the proxy we started)
#   scripts/proxy/proxy-shared.sh status    # show /health + pidfile state
#   scripts/proxy/proxy-shared.sh restart   # stop then start (accumulated routes persist)
#
# Env (defaults resolved in scripts/proxy/proxy-env.sh):
#   PROXY_PORT=N           Fixed port (default: 3456)
#   PROXY_MAX_CONCURRENT=N Upstream concurrency, baked in at start (default: 64)
#
# This script sources .env so the proxy process inherits all backend
# url_env/key_env. Routes added later are resolved against the proxy's own
# environment (at each /admin/reload), so any backend whose env var is missing at
# start time will resolve to an empty URL/key for routes added afterward.
set -e

# ── Resolve paths ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."
REPO_ROOT="$(pwd)"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# shellcheck source=scripts/proxy/proxy-env.sh
source "$SCRIPT_DIR/proxy-env.sh"
SHARED_DIR="$PROXY_LOG_DIR"
PIDFILE="$SHARED_DIR/shared-proxy.pid"
STDLOG="$SHARED_DIR/shared-proxy.log"

mkdir -p "$SHARED_DIR"

health_ok() {
    curl -s --max-time 2 "http://localhost:$PROXY_PORT/health" 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status")=="ok" else 1)' \
        2>/dev/null
}

# True when PIDFILE holds a live PID whose args reference OUR shared config (so
# we never kill an unrelated process that happens to reuse the PID/port).
shared_pid() {
    [ -f "$PIDFILE" ] || return 1
    local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    local cmd; cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    printf '%s' "$cmd" | grep -F -- "--config $SHARED_CONFIG" >/dev/null 2>&1 || return 1
    echo "$pid"
}

cmd_start() {
    # Load .env so the proxy inherits backend credentials for every route added
    # later (resolved against THIS process's env at each reload).
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a; source "$REPO_ROOT/.env"; set +a
        echo "Loaded environment from .env"
    fi

    if health_ok; then
        echo "Shared proxy already running on :$PROXY_PORT"
        return 0
    fi
    if fuser "$PROXY_PORT/tcp" >/dev/null 2>&1; then
        echo "ERROR: port :$PROXY_PORT is busy but not a healthy shared proxy." >&2
        echo "  Something else owns the port; stop it or set PROXY_PORT=N." >&2
        return 1
    fi

    # Seed an empty-routes config if absent. The proxy boots fine with zero
    # routes (unmatched requests 404 until run.sh adds one). max_concurrent is
    # baked in here for the proxy's whole lifetime; restart to change it.
    if [ ! -s "$SHARED_CONFIG" ]; then
        python3 - "$SHARED_CONFIG" "$PROXY_PORT" "$SHARED_DIR" "$PROXY_MAX_CONCURRENT" <<'PY'
import sys, yaml
path, port, log_dir, mc = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
cfg = {"proxy": {
    "host": "0.0.0.0", "port": port, "log_dir": log_dir,
    "log_enabled": False, "max_concurrent": mc, "shared": True, "routes": [],
}}
open(path, "w").write(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
PY
        echo "Seeded empty shared config: $SHARED_CONFIG"
    fi

    nohup python3 -m workbuddy_bench.proxy \
        --config "$SHARED_CONFIG" --port "$PROXY_PORT" --log-dir "$SHARED_DIR" \
        >> "$STDLOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    disown "$pid" 2>/dev/null || true

    local deadline=$(( SECONDS + 20 ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if health_ok; then
            echo "Shared proxy started (PID=$pid, port=:$PROXY_PORT, log=$STDLOG)"
            return 0
        fi
        sleep 0.5
    done
    echo "ERROR: shared proxy did not become healthy within 20s." >&2
    echo "  Debug: tail -n 50 $STDLOG" >&2
    return 1
}

cmd_stop() {
    local pid; pid="$(shared_pid || true)"
    if [ -z "$pid" ]; then
        echo "Shared proxy not running (no live PID matching $SHARED_CONFIG)."
        rm -f "$PIDFILE"
        return 0
    fi
    echo "Stopping shared proxy PID=$pid"
    kill "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
}

cmd_status() {
    if health_ok; then
        echo "Shared proxy: HEALTHY on :$PROXY_PORT"
        curl -s --max-time 2 "http://localhost:$PROXY_PORT/health" 2>/dev/null \
            | python3 -m json.tool 2>/dev/null || true
    else
        echo "Shared proxy: NOT responding on :$PROXY_PORT"
    fi
    local pid; pid="$(shared_pid || true)"
    if [ -n "$pid" ]; then
        echo "pidfile: $PIDFILE -> PID $pid (live, ours)"
    elif [ -f "$PIDFILE" ]; then
        echo "pidfile: $PIDFILE -> stale (PID not live or not ours)"
    else
        echo "pidfile: none"
    fi
    echo "config: $SHARED_CONFIG"
}

case "${1:-}" in
    start)   cmd_start;;
    stop)    cmd_stop;;
    status)  cmd_status;;
    restart) cmd_stop; cmd_start;;
    *)
        echo "Usage: $0 {start|stop|status|restart}" >&2
        exit 1;;
esac
