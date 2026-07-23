#!/usr/bin/env python3
"""Proxy — Pipeline-based universal protocol proxy.

Supports:
- True passthrough (zero protocol modification)
- Anthropic->OpenAI protocol mapping (optional per-route)
- OpenAI passthrough with body injection
- Stream and non-stream for all modes
- Composable interceptors (logging, body injection)
- JSONL logging compatible with log viewer
- Session tracking with periodic progress summaries
- Request token route selection: Authorization/X-API key material selects a
  proxy route when it matches a route slug; each route owns its upstream key.

Usage:
    python3 -m workbuddy_bench.proxy --config config.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import (
    ProxyConfig,
    ProxyMode,
    RouteConfig,
    load_config_from_yaml,
)
from .interceptors import RequestContext
from .pipeline import Pipeline, _is_inference_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_config: ProxyConfig | None = None
_pipeline: Pipeline | None = None
# Path the proxy was launched with (--config), so /admin/reload can re-read the
# SAME file. Set in main() before the config is first loaded.
_config_path: Path | None = None

def _extract_bench_context(trial_from_token: str = "") -> dict[str, str]:
    """Build the per-request bench metadata for logging.

    ``trial_from_token`` is the trial id split off the bearer token
    (``{trial_id}::{route}``, produced by the harness). It is the sole source of
    per-trial attribution; experiment/harness fall back to proxy config defaults.
    The logger uses ``trial_id`` and falls back to the route's run-level
    instance_id when it is empty (legacy bare-token requests).
    """
    return {
        "experiment": _config.default_experiment if _config else "",
        "task_id": trial_from_token,
        "trial_id": trial_from_token,
        "harness": _config.default_harness if _config else "",
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="WorkBuddy Bench Proxy")


@app.on_event("startup")
async def _startup():
    global _pipeline
    if _config:
        _pipeline = Pipeline(_config)
        log.info(
            "Pipeline ready: %d routes (concurrency limited per-model, no global cap)",
            len(_config.routes),
        )
        for slug, route in _config.routes.items():
            mc = route.backend.max_concurrent
            log.info("  route: %s -> %s (mode=%s, max_concurrent=%s)",
                     slug, route.backend.url, route.mode.value,
                     mc if mc else "unlimited")


@app.on_event("shutdown")
async def _shutdown():
    if _pipeline:
        await _pipeline.close()


def _bearer_token(request: Request) -> str:
    """Return the request token used for route selection, if present."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        parts = auth.split(None, 1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if token:
            return token
    return (
        request.headers.get("x-api-key", "").strip()
        or request.headers.get("anthropic-auth-token", "").strip()
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _routes_info() -> dict[str, dict[str, str]]:
    """Per-route summary used by /health and /admin/reload responses."""
    if not _config:
        return {}
    return {
        slug: {
            "mode": r.mode.value,
            "client_protocol": r.client_protocol,
            "backend_protocol": r.backend_protocol,
            "backend": r.backend.url,
        }
        for slug, r in _config.routes.items()
    }


@app.get("/")
@app.get("/health")
async def health(request: Request):
    """Health check and status."""
    return JSONResponse(content={
        "status": "ok",
        "routes": _routes_info(),
        "active_requests": _pipeline.sender.active_requests if _pipeline else 0,
    })


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    """List available models/routes."""
    models = []
    if _config:
        for slug in sorted(_config.routes.keys()):
            models.append({"id": slug, "object": "model"})
    return JSONResponse(content={"data": models, "object": "list"})


@app.post("/admin/reload")
async def reload_config(request: Request):
    """Hot-reload routes from the config file the proxy was launched with.

    Re-reads ``_config_path`` and atomically rebinds ``_config.routes``; the
    pipeline holds the same config object by reference, so new routes are visible
    immediately. In-flight requests already captured their own effective route
    and are unaffected.

    Reloads only the route table. Startup-only settings (``max_concurrent``,
    ``log_dir`` / ``log_enabled``, ``host`` / ``port`` / ``backend_timeout`` /
    ``verify_tls``) need a restart to change.

    On any load error the existing routes are left untouched (the swap only
    happens after a successful parse), and a 400 is returned.
    """
    if _config is None or _config_path is None:
        return JSONResponse(
            status_code=409,
            content={"error": "proxy was not started from a config file; cannot reload"},
        )
    try:
        new_cfg = load_config_from_yaml(_config_path)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        log.warning("Reload rejected: %s", exc)
        return JSONResponse(
            status_code=400,
            content={"error": "reload failed; routes unchanged", "detail": str(exc)},
        )
    old_count = len(_config.routes)
    # Single atomic rebind (GIL-atomic); never clear-in-place (would expose a
    # partially-rebuilt dict to a concurrent resolve_route).
    _config.routes = new_cfg.routes
    log.info("Reloaded routes: %d -> %d", old_count, len(_config.routes))
    for slug, route in _config.routes.items():
        log.info("  route: %s -> %s (mode=%s)", slug, route.backend.url, route.mode.value)
    return JSONResponse(content={"status": "ok", "routes": _routes_info()})


@app.post("/v1/chat/completions")
async def handle_openai(request: Request):
    """Handle OpenAI-format requests (passthrough or with conversion)."""
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    return await _process_request(request, body, body_bytes, is_openai_client=True)


@app.post("/{path:path}")
async def handle_any(path: str, request: Request):
    """Handle any POST -- used for Anthropic-format requests."""
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON"},
        })

    return await _process_request(request, body, body_bytes, is_openai_client=False)


async def _process_request(
    request: Request,
    body: dict[str, Any],
    body_bytes: bytes,
    is_openai_client: bool,
) -> JSONResponse | StreamingResponse:
    """Unified request processing for both OpenAI and Anthropic clients."""
    if not _pipeline:
        return JSONResponse(status_code=503, content={"error": "Pipeline not initialized"})

    # Resolve route
    model = body.get("model", "")
    route_token = _bearer_token(request)
    # The harness prefixes the per-trial session id onto the token as
    # ``{trial_id}::{route}``. Split it off before route resolution: the route
    # half must stay a bare slug for the strict route-slug match, while the trial
    # half feeds per-trial log attribution. A token without ``::`` is the legacy
    # bare route token, left whole.
    trial_from_token = ""
    if route_token and "::" in route_token:
        trial_from_token, route_token = route_token.split("::", 1)
    route = None
    if route_token:
        route = _pipeline.resolve_route(route_token, str(request.url.path))
    if not route:
        route = _pipeline.resolve_route(model, str(request.url.path))
    if not route:
        available = sorted(_config.routes.keys()) if _config else []
        error_body = {"error": {
            "type": "not_found_error",
            "message": f"Model '{model}' not found. Available: {available}",
        }}
        return JSONResponse(status_code=404, content=error_body)

    # Determine effective mode/protocols. A route generated for Anthropic->OpenAI
    # conversion can also serve OpenAI-native clients as same-protocol passthrough;
    # all other protocol mismatches are explicit errors until O2A exists.
    incoming_protocol = "openai" if is_openai_client else "anthropic"
    effective_mode = route.mode
    client_protocol = route.client_protocol
    backend_protocol = route.backend_protocol
    # A same-protocol passthrough route is a transparent relay: it must forward
    # auxiliary upstream endpoints (/v1/models, /v1/embeddings, /health, ...) as-is.
    # Those non-inference requests carry no protocol-specific body the proxy parses,
    # so the protocol-mismatch guard (which only matters for inference conversion)
    # does not apply — the path is simply relayed under the route's own protocol.
    is_passthrough = route.mode == ProxyMode.PASSTHROUGH
    # Classify the path by the incoming request's protocol, not the route's — else a
    # cross-protocol inference call (e.g. /v1/messages hitting an OpenAI route) is
    # misread as a non-inference endpoint and skips the mismatch guard below.
    is_inference = _is_inference_path(str(request.url.path), incoming_protocol)
    if is_openai_client and route.mode == ProxyMode.A2O:
        effective_mode = ProxyMode.PASSTHROUGH
        client_protocol = "openai"
        backend_protocol = "openai"
    elif is_passthrough and not is_inference:
        # Transparent relay of a non-inference endpoint: keep the route's protocol.
        incoming_protocol = route.client_protocol
    elif incoming_protocol != route.client_protocol:
        error_body = {"error": {
            "type": "unsupported_protocol_error",
            "message": (
                f"Route '{route.slug}' expects {route.client_protocol} client protocol; "
                f"got {incoming_protocol}. OpenAI->Anthropic conversion is not implemented."
            ),
        }}
        return JSONResponse(status_code=400, content=error_body)

    # Use a route override with the effective mode/protocol metadata.
    effective_route = RouteConfig(
        slug=route.slug,
        mode=effective_mode,
        backend=route.backend,
        backend_model=route.backend_model,
        client_protocol=client_protocol,
        backend_protocol=backend_protocol,
        injected_params=route.injected_params,
        interceptors=route.interceptors,
        instance_id=route.instance_id,
        dedup_reasoning=route.dedup_reasoning,
        reasoning_passback=route.reasoning_passback,
    )

    # Extract bench context
    bench_ctx = _extract_bench_context(trial_from_token)

    # Snapshot the client body for logging from the parsed body, decoupled via a
    # round-trip so later in-place mutations don't leak back into the log.
    client_body_snapshot = json.loads(json.dumps(body)) if body else {}

    # Build request context
    is_stream = body.get("stream", False)
    ctx = RequestContext(
        request_id=f"req_{uuid.uuid4().hex[:16]}",
        method="POST",
        path=str(request.url.path),
        headers=dict(request.headers),
        query_params=dict(request.query_params),
        route=effective_route,
        mode=effective_mode,
        raw_body=body_bytes,
        parsed_body=body,
        client_body=client_body_snapshot,
        is_stream=is_stream,
        meta=bench_ctx,
    )

    log.info("[%s] %s model=%s mode=%s protocol=%s->%s stream=%s",
             ctx.request_id, "OAI" if is_openai_client else "Anthropic",
             model, effective_mode.value, client_protocol, backend_protocol, is_stream)

    if is_stream:
        return StreamingResponse(
            _pipeline.handle_stream(ctx),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        result = await _pipeline.handle_request(ctx)
        return JSONResponse(content=result.body, status_code=result.status)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    global _config, _config_path

    import uvicorn

    parser = argparse.ArgumentParser(
        description="WorkBuddy Bench Proxy -- Pipeline-based universal protocol proxy"
    )

    parser.add_argument("--config", required=True, help="YAML config file path")
    parser.add_argument("--port", type=int, default=3456)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--log-dir", default="", help="JSONL log output directory")
    parser.add_argument("--no-log", dest="log_enabled", action="store_false",
                        help="Disable request/response logging (default: logging on)")
    parser.set_defaults(log_enabled=True)
    parser.add_argument("--max-concurrent", type=int, default=16,
                        help="Max concurrent requests to backend (default: 16)")
    parser.add_argument("--backend-timeout", type=int, default=600,
                        help="Backend request timeout in seconds (default: 600)")
    parser.add_argument("--backend-retries", type=int, default=10,
                        help="Max retries on 429/502/503/504/timeout (default: 10)")
    parser.add_argument("--retry-base-delay", type=int, default=30,
                        help="Base delay in seconds between retries (default: 30)")
    parser.add_argument("--verify-tls", action="store_true",
                        help="Verify backend TLS certificates (default: off; enable for public endpoints)")
    parser.add_argument("--default-experiment", default="",
                        help="Fallback experiment tag for JSONL logs")
    parser.add_argument("--default-harness", default="",
                        help="Fallback harness tag for JSONL logs")

    args = parser.parse_args()

    _config_path = Path(args.config)
    _config = load_config_from_yaml(_config_path)

    # Override config-file values from CLI only when explicitly set.
    if args.port != 3456:
        _config.port = args.port
    if args.host != "0.0.0.0":
        _config.host = args.host
    if args.log_dir:
        _config.log_dir = args.log_dir
    # --no-log force-disables logging regardless of the config-file value. The
    # default (True) never re-enables a config that set log_enabled: false.
    if not args.log_enabled:
        _config.log_enabled = False
    if args.verify_tls:
        _config.verify_tls = True

    log.info(
        "Proxy starting: host=%s port=%d routes=%d log_dir=%s",
        _config.host, _config.port, len(_config.routes),
        (_config.log_dir or "(none)") if _config.log_enabled else "(disabled)",
    )

    uvicorn.run(app, host=_config.host, port=_config.port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
