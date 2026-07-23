# Shared proxy endpoint/path defaults, sourced by run.sh, run-judge.sh and
# proxy-shared.sh so the port, log dir, shared-config path and docker-reachable
# host are defined in ONE place instead of re-hardcoded per script.
#
# Sourced, not executed. The caller must have REPO_ROOT set. Every value honors a
# pre-set env override (PROXY_PORT=N scripts/run.sh ... still works).
#
# Exposed:
#   PROXY_PORT          proxy listen port (default 3456)
#   PROXY_LOG_DIR       proxy log + shared-config dir ($REPO_ROOT/scripts/logs/proxy)
#   SHARED_CONFIG       shared multi-route config path ($PROXY_LOG_DIR/shared-proxy.yaml)
#   PROXY_HOST          host a docker container uses to reach the host proxy
#                       (default host.docker.internal; override for non-Docker runtimes)
#   PROXY_MAX_CONCURRENT upstream concurrency baked in at proxy start (default 64)
#
# NOTE: proxy-shared.sh and run.sh identify "their" proxy process by grepping the
# live cmdline for "--config $SHARED_CONFIG" / "--config $PROXY_CONFIG". Keep the
# SHARED_CONFIG string byte-identical to what those greps expect — changing how
# this path is spelled would make the liveness check miss a running proxy.

: "${REPO_ROOT:?proxy-env.sh requires REPO_ROOT to be set before sourcing}"

PROXY_PORT="${PROXY_PORT:-3456}"
PROXY_LOG_DIR="${PROXY_LOG_DIR:-$REPO_ROOT/scripts/logs/proxy}"
SHARED_CONFIG="${SHARED_CONFIG:-$PROXY_LOG_DIR/shared-proxy.yaml}"
PROXY_HOST="${PROXY_HOST:-host.docker.internal}"
PROXY_MAX_CONCURRENT="${PROXY_MAX_CONCURRENT:-64}"
