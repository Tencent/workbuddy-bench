"""Proxy configuration and route definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class ProxyMode(str, Enum):
    """How the proxy handles protocol between client and backend."""
    PASSTHROUGH = "passthrough"           # Same-protocol forwarding
    A2O = "a2o"                           # Anthropic client -> OpenAI backend
    O2A = "o2a"                           # OpenAI client -> Anthropic backend


# Proxy → backend retry policy, overridable via environment (read once at import).
# These set the default baked into ProxyConfig; a YAML proxy block or per-route
# backend field can still override them. Retries are on by default and use a
# fixed interval (rationale in proxy/sender.py::_retry_delay). Set
# WBBENCH_PROXY_MAX_RETRIES=0 to disable retries entirely.
#   WBBENCH_PROXY_MAX_RETRIES     integer, default 6  (window = retries * delay)
#   WBBENCH_PROXY_RETRY_DELAY_MS  ms,      default 5000 (5s fixed interval)
# Default window: 6 * 5s = 30s, safely < cbc's 600s stream/first-token timeout.
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is not None:
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return default


def _env_float_ms_to_s(name: str, default_s: float) -> float:
    raw = os.environ.get(name)
    if raw is not None:
        try:
            val = float(raw)
            if val > 0:
                return val / 1000.0
        except ValueError:
            pass
    return default_s


_DEFAULT_BACKEND_RETRIES = _env_int("WBBENCH_PROXY_MAX_RETRIES", 6)
_DEFAULT_RETRY_DELAY = _env_float_ms_to_s("WBBENCH_PROXY_RETRY_DELAY_MS", 5.0)


@dataclass(frozen=True)
class BackendConfig:
    """Upstream backend connection info."""
    url: str
    key: str = ""
    timeout: float = 600.0
    max_retries: int = 3
    retry_delay: float = 5.0
    # Optional per-model in-flight cap. None = unlimited (the proxy does not
    # throttle this model; concurrency is bounded upstream by the eval job's
    # container/trial limits). Set via model.max_concurrent in configs/models.
    max_concurrent: int | None = None
    # Optional custom headers injected into every upstream request (literal
    # values). Set via model.backend_headers in configs/models. Reserved headers
    # (host/content-length/authorization/x-api-key) are ignored by the sender so
    # a misconfigured header can't break auth or transport framing.
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteConfig:
    """A single proxy route -- maps a model/path to a backend with a mode."""
    slug: str
    mode: ProxyMode
    backend: BackendConfig
    backend_model: str = ""           # Model name to send to backend (if different)
    client_protocol: str = "openai"
    backend_protocol: str = "openai"
    # Params injected (shallow-update) into the upstream request body. The runner
    # (resolve_manifest) flattens the model's ``params`` block into this before
    # writing the proxy config, so it holds top-level sampling params (temperature,
    # top_p, ...) plus the contents of the ``params.extra_body`` sub-block — not
    # just the extra_body sub-block. The YAML / runner config key remains
    # ``extra_body`` (external contract), mapped to this field by
    # load_config_from_yaml.
    injected_params: dict[str, Any] = field(default_factory=dict)
    interceptors: list[str] = field(default_factory=lambda: ["log"])
    # Per-trial identifier (set when record_full_io is on). Lets the logger tag
    # each request and split logs per trial, so proxy logs can be tied back to the
    # trial's results/<trial>/config.json (which carries the same instance_id).
    instance_id: str = ""
    # Backend-quirk workarounds, both default-off so unaffected routes stay a
    # zero-parse byte passthrough. Some OpenAI-compatible backends emit the same
    # thinking text under both `reasoning` and `reasoning_content` on every chunk,
    # and only honor cross-turn thinking passback via `reasoning_content` (which
    # cbc flattens away to `reasoning`).
    dedup_reasoning: bool = False       # response side: drop the duplicated key
    reasoning_passback: bool = False    # request side: synthesize reasoning_content

    @property
    def effective_model(self) -> str:
        return self.backend_model or self.slug

    @property
    def extra_body(self) -> dict[str, Any]:
        """Alias for ``injected_params`` (the external config key is ``extra_body``)."""
        return self.injected_params


@dataclass
class ProxyConfig:
    """Top-level proxy configuration."""
    host: str = "0.0.0.0"
    port: int = 3456
    log_dir: str = ""
    # Master switch for request/response logging. Default on ("store logs").
    # When false, the pipeline does not register the LogInterceptor at all, so
    # nothing is written even if a log_dir is configured (e.g. --no-log).
    log_enabled: bool = True
    max_concurrent: int = 16
    backend_timeout: float = 600.0
    # Fixed-interval retry (not exponential). Defaults come from env
    # (WBBENCH_PROXY_MAX_RETRIES / WBBENCH_PROXY_RETRY_DELAY_MS); YAML overrides.
    backend_retries: int = _DEFAULT_BACKEND_RETRIES
    retry_base_delay: float = _DEFAULT_RETRY_DELAY
    verify_tls: bool = False
    # Shared long-lived proxy: routes accumulate across jobs. When true,
    # resolve_route must NOT use the single-route fallback (every request must
    # match a slug by token/model, else 404) — otherwise, during the window when
    # only one route is loaded, a request for any other model would be silently
    # answered by that one route. Job-private proxies (one route, shared=False)
    # keep the fallback so a harness whose body.model/token != slug still routes.
    shared: bool = False
    routes: dict[str, RouteConfig] = field(default_factory=dict)
    default_experiment: str = ""
    default_harness: str = ""


def load_config_from_yaml(config_path: Path) -> ProxyConfig:
    """Load proxy config from a YAML file."""
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a YAML mapping, got {type(raw)}")

    proxy_block = raw.get("proxy", raw)

    config = ProxyConfig(
        host=proxy_block.get("host", "0.0.0.0"),
        port=proxy_block.get("port", 3456),
        log_dir=proxy_block.get("log_dir", ""),
        log_enabled=bool(proxy_block.get("log_enabled", True)),
        max_concurrent=proxy_block.get("max_concurrent", 16),
        backend_timeout=proxy_block.get("backend_timeout", 600.0),
        backend_retries=proxy_block.get("backend_retries", _DEFAULT_BACKEND_RETRIES),
        retry_base_delay=proxy_block.get("retry_base_delay", _DEFAULT_RETRY_DELAY),
        verify_tls=proxy_block.get("verify_tls", False),
        shared=bool(proxy_block.get("shared", False)),
        default_experiment=proxy_block.get("default_experiment", ""),
        default_harness=proxy_block.get("default_harness", ""),
    )

    for route_raw in proxy_block.get("routes", []):
        backend_raw = route_raw.get("backend", {})
        # Resolve env var references
        url = _resolve_env(backend_raw.get("url", ""), backend_raw.get("url_env", ""))
        key = _resolve_env(backend_raw.get("key", ""), backend_raw.get("key_env", ""))

        backend = BackendConfig(
            url=url,
            key=key,
            timeout=backend_raw.get("timeout", config.backend_timeout),
            max_retries=backend_raw.get("max_retries", config.backend_retries),
            retry_delay=backend_raw.get("retry_delay", config.retry_base_delay),
            # None (key absent) = unlimited for this model.
            max_concurrent=backend_raw.get("max_concurrent"),
            headers={
                str(k): str(v)
                for k, v in (backend_raw.get("headers") or {}).items()
            },
        )

        # ``openai_passthrough`` was a distinct OpenAI-only mode; internally it is
        # generic passthrough plus openai/openai protocol metadata (defaulted
        # below). Normalize the legacy literal before constructing the enum so old
        # generated route configs still load.
        raw_mode_str = route_raw.get("mode", "passthrough")
        if raw_mode_str == "openai_passthrough":
            raw_mode_str = "passthrough"
        raw_mode = ProxyMode(raw_mode_str)
        mode = raw_mode
        if raw_mode == ProxyMode.O2A:
            # O2A (OpenAI client -> Anthropic backend) conversion is not
            # implemented in the pipeline; accepting it here would silently
            # forward an unconverted OpenAI body to an Anthropic backend. Fail
            # fast with a clear error instead of producing garbage responses.
            raise ValueError(
                f"route {route_raw.get('slug', '?')!r}: mode 'o2a' is not "
                "supported (OpenAI->Anthropic conversion is unimplemented)"
            )
        if raw_mode == ProxyMode.A2O:
            default_client_protocol = "anthropic"
            default_backend_protocol = "openai"
        else:
            default_client_protocol = "openai"
            default_backend_protocol = "openai"
        client_protocol = str(route_raw.get("client_protocol") or default_client_protocol)
        backend_protocol = str(route_raw.get("backend_protocol") or default_backend_protocol)
        slug = route_raw["slug"]

        route = RouteConfig(
            slug=slug,
            mode=mode,
            backend=backend,
            backend_model=route_raw.get("backend_model", ""),
            client_protocol=client_protocol,
            backend_protocol=backend_protocol,
            # YAML/runner key stays ``extra_body`` (external contract); mapped to
            # the accurately-named internal field.
            injected_params=route_raw.get("extra_body", {}),
            interceptors=route_raw.get("interceptors", ["log"]),
            instance_id=str(route_raw.get("instance_id", "") or ""),
            dedup_reasoning=bool(route_raw.get("dedup_reasoning", False)),
            reasoning_passback=bool(route_raw.get("reasoning_passback", False)),
        )
        config.routes[slug] = route

    return config


def _resolve_env(direct: str, env_name: str) -> str:
    """Resolve a value: direct value takes priority, then env var."""
    if direct:
        return direct
    if env_name:
        return os.environ.get(env_name, "")
    return ""
