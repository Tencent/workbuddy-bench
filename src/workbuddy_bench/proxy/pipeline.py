"""Core pipeline -- orchestrates routing, interception, protocol mapping, and forwarding."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator

from .config import ProxyMode, RouteConfig, ProxyConfig
from .interceptors import RequestContext, ResponseContext, StreamChunk
from .interceptors.logger import LogInterceptor
from .interceptors.extra_body import ExtraBodyInterceptor
from .protocols.a2o import (
    build_tool_name_mapping,
    convert_request_a2o,
    convert_response_o2a,
    StreamingA2OConverter,
)
from .sender import UpstreamSender, UpstreamError

log = logging.getLogger("proxy.pipeline")

# Matches a trailing API-version segment like "/v1" or "/v2beta".
_API_VERSION_RE = re.compile(r"/v\d+\w*$")

# Anthropic-only request headers that are meaningless — and occasionally
# rejected — by OpenAI-compatible backends. Dropped when forwarding an
# Anthropic client request to a non-Anthropic backend (A2O / OpenAI passthrough).
_ANTHROPIC_ONLY_HEADERS = frozenset({
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "x-api-key",
    "anthropic-auth-token",
})


def _scrub_anthropic_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip Anthropic-only headers before sending to an OpenAI-style backend."""
    return {k: v for k, v in headers.items() if k.lower() not in _ANTHROPIC_ONLY_HEADERS}


def _strip_cache_control(obj: Any) -> None:
    """Recursively remove every ``cache_control`` field from a request body.

    Only for the A2O path. ``cache_control`` is Anthropic prompt-caching metadata:
    the client marks blocks it wants cached, and Anthropic caps a request at 4 such
    blocks. On the a2o→OpenAI link the backend gateway injects its own cache_control
    breakpoints (observed already at the 4-block cap), so any breakpoint the client
    (e.g. Claude Code's system prompt) still carries pushes the total over the limit
    and the backend rejects the whole request with 400 ``Found 5``. Caching on this
    link is decided by the gateway/backend, not the client, so the client's
    cache_control has no useful effect here — stripping it prevents the overflow
    without losing any cache hit. Passthrough (native Anthropic) keeps it.
    """
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for value in obj.values():
            _strip_cache_control(value)
    elif isinstance(obj, list):
        for item in obj:
            _strip_cache_control(item)


def _is_inference_path(path: str, protocol: str) -> bool:
    """True if ``path`` is the primary inference endpoint *for ``protocol``*.

    ``/chat/completions`` for OpenAI, ``/v1/messages`` for Anthropic. Auxiliary
    endpoints (``/v1/models``, ``/health``, ...) return False so they're relayed
    untouched.

    The caller chooses which protocol to pass, and the two call sites pass
    different ones on purpose — do not "unify" them: the protocol guard passes
    the incoming request protocol (classify the client's path before the
    mismatch check); the injection gate passes the route's backend protocol
    (classify the upstream endpoint being built). They coincide for
    same-protocol passthrough; A2O is normalized to passthrough before reaching
    the injection gate.
    """
    p = (path or "").rstrip("/")
    if protocol == "anthropic":
        return p.endswith("/messages")
    return p.endswith("/chat/completions")


def _chat_completions_url(base_url: str) -> str:
    """Normalize a backend base URL to its OpenAI chat-completions endpoint.

    Single source of truth for OpenAI-compatible upstream URL building:
      - ``https://h/v1``                 -> ``https://h/v1/chat/completions``
      - ``https://h``                    -> ``https://h/v1/chat/completions``
      - ``https://h/v1/chat/completions``-> unchanged (idempotent)
      - ``https://h/openai/v1``          -> ``https://h/openai/v1/chat/completions``
    """
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if _API_VERSION_RE.search(url):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _same_protocol_url(base_url: str, request_path: str) -> str:
    """Join a backend base URL with the incoming same-protocol request path.

    Anthropic passthrough must preserve paths such as ``/v1/messages`` instead of
    normalizing to OpenAI ``/chat/completions``. The helper also avoids duplicating
    API-version segments when the backend URL already ends in ``/v1``.

    When the backend URL is already pinned to a concrete inference endpoint (ends
    in ``/chat/completions`` or ``/messages``), the client's own endpoint suffix
    is redundant and is dropped so the base wins verbatim. This lets a backend
    that serves at a non-standard path (e.g. ``/openapi/chat/completions`` with no
    ``/v1`` segment) be reached by pinning the full endpoint in the base URL,
    regardless of the ``/v1/...`` path the client library hardcodes.
    """
    url = base_url.rstrip("/")
    path = "/" + request_path.lstrip("/") if request_path else ""
    if not path or path == "/":
        return url
    if url.endswith(path):
        return url
    for endpoint in ("/chat/completions", "/messages"):
        if url.endswith(endpoint) and path.endswith(endpoint):
            return url
    version = _API_VERSION_RE.search(url)
    if version and path.startswith(f"{version.group(0)}/"):
        return url + path[len(version.group(0)):]
    return url + path


def _upstream_error_payload(exc: UpstreamError, *, anthropic: bool = False) -> dict[str, Any]:
    """Build a client-safe error payload while preserving backend diagnostics.

    ``anthropic=True`` wraps it in Anthropic's error envelope
    (``{"type": "error", "error": {...}}``) so an Anthropic client (a2o /
    anthropic passthrough) gets a body its SDK can parse; otherwise the OpenAI
    shape (``{"error": {...}}``).
    """
    error: dict[str, Any] = {
        "type": "api_error",
        "message": str(exc),
        "upstream_status": exc.status,
    }
    if exc.body:
        error["upstream_body"] = exc.body
    if anthropic:
        return {"type": "error", "error": error}
    return {"error": error}


def _upstream_error_context(exc: UpstreamError, duration_ms: float) -> ResponseContext:
    """Build the logging/interceptor response context for upstream failures."""
    return ResponseContext(
        status_code=exc.status,
        error=str(exc),
        upstream_error_body=exc.body or None,
        duration_ms=duration_ms,
    )


def _anthropic_error_event(exc: UpstreamError) -> bytes:
    payload = {"type": "error", "error": _upstream_error_payload(exc)["error"]}
    return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()


def _openai_error_event(exc: UpstreamError) -> bytes:
    return f"data: {json.dumps(_upstream_error_payload(exc))}\n\n".encode()


def _dedup_reasoning(obj: dict[str, Any]) -> bool:
    """Drop the duplicate ``reasoning_content`` when a message/delta carries both
    ``reasoning`` and ``reasoning_content``.

    Some OpenAI-compatible backends emit the same thinking text under both keys
    on every chunk, so a client that concatenates all reasoning keys sees it
    twice. Keep ``reasoning`` (what cbc flattens to) and drop
    ``reasoning_content``. Returns True if it removed the field.
    """
    if obj.get("reasoning") and obj.get("reasoning_content"):
        obj.pop("reasoning_content", None)
        return True
    return False


def _dedup_reasoning_payload(payload: dict[str, Any]) -> bool:
    """Apply :func:`_dedup_reasoning` to each choice's delta (stream) or message
    (non-stream) in an OpenAI chat-completion(.chunk) payload. Returns True if
    any choice was changed."""
    changed = False
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        target = choice.get("delta")
        if not isinstance(target, dict):
            target = choice.get("message")
        if isinstance(target, dict) and _dedup_reasoning(target):
            changed = True
    return changed


def _rewrite_reasoning_sse(chunk: bytes) -> bytes:
    """Rewrite an OpenAI SSE byte chunk to drop duplicate ``reasoning_content``.

    Conservative and per-line: only fully-parseable single-line ``data:`` JSON
    payloads are rewritten; ``[DONE]``, keep-alive comments, and anything that
    doesn't ``json.loads`` (e.g. a JSON event split across chunk boundaries) pass
    through verbatim. When nothing changed, the original bytes object is returned
    unchanged (fast path). We never buffer across chunks — a straddling event
    keeps its duplicate, which the client flattens anyway.
    """
    text = chunk.decode("utf-8", errors="replace")
    out: list[str] = []
    changed = False
    for line in text.split("\n"):
        if not line.startswith("data:"):
            out.append(line)
            continue
        data = line[len("data:"):].lstrip()
        if not data or data == "[DONE]":
            out.append(line)
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(payload, dict) and _dedup_reasoning_payload(payload):
            out.append("data: " + json.dumps(payload, ensure_ascii=False))
            changed = True
        else:
            out.append(line)
    if not changed:
        return chunk
    return "\n".join(out).encode("utf-8")


def _inject_reasoning_passback(body: dict[str, Any]) -> None:
    """Synthesize ``reasoning_content`` from ``reasoning`` on outgoing messages.

    Some OpenAI-compatible backends only honor cross-turn thinking passback when
    the prior assistant message carries ``reasoning_content``, but cbc flattens
    all thinking to ``reasoning``. Copy ``reasoning`` -> ``reasoning_content``
    when the latter is absent/empty so passback works. Truthiness handles both
    the absent and explicit-``null`` cases.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        reasoning = msg.get("reasoning")
        if reasoning and not msg.get("reasoning_content"):
            msg["reasoning_content"] = reasoning


class Pipeline:
    """The main processing pipeline.

    For each request:
    1. Route resolution
    2. Request interceptors (logging, body injection)
    3. Protocol mapping (if needed)
    4. Upstream send (stream or non-stream)
    5. Response interceptors (logging)
    6. Return to client

    The proxy only converts protocols and injects model-configured request
    params (extra_body); it never truncates tool calls or overrides the
    client's max_tokens.
    """

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.sender = UpstreamSender(
            default_timeout=config.backend_timeout,
            verify_tls=config.verify_tls,
        )

        # Build interceptor instances. Logging is registered only when enabled
        # (default on); --no-log / proxy.log_enabled: false drops it entirely, so
        # the "log" name in a route's interceptor list becomes a harmless no-op.
        self._interceptors: dict[str, Any] = {}
        self._log_interceptor: LogInterceptor | None = None
        if config.log_enabled:
            self._log_interceptor = LogInterceptor(config.log_dir)
            self._interceptors["log"] = self._log_interceptor

        self._extra_body_interceptor = ExtraBodyInterceptor()
        self._interceptors["inject_extra_body"] = self._extra_body_interceptor

    def resolve_route(self, model: str, path: str = "") -> RouteConfig | None:
        """Find the matching route for a request.

        Strict-match semantics (since 2026-06-29): a NON-EMPTY ``model`` that
        doesn't match an exact slug returns ``None`` (→ 404). The single-route
        fallback only fires when ``model`` is empty — i.e. the caller had no
        value to look up at all. This makes config typos visible instead of
        silently routing to "whatever the one route is" (a common job-private
        foot-gun). The fallback exists for the legitimate case where a harness
        sends an empty/missing token AND an empty/missing body.model and we
        still need to pick the only sensible route.
        """
        # Direct model match
        if model in self.config.routes:
            return self.config.routes[model]

        # A non-empty value that didn't match is a misconfiguration — return
        # None so the caller can surface a clean 404 with the available routes,
        # rather than silently routing to whichever single route exists.
        if model:
            return None

        # Empty input: fall back to the single route in job-private mode (the
        # legitimate "no addressing info from the harness" case). Shared proxies
        # never fall back — accumulated routes would mean any unmatched request
        # could be misrouted to the most-recently-loaded one.
        if not self.config.shared and len(self.config.routes) == 1:
            return next(iter(self.config.routes.values()))

        return None

    async def handle_request(
        self,
        ctx: RequestContext,
    ) -> "PipelineResult":
        """Process a complete non-streaming request through the pipeline."""
        route = ctx.route
        if not route:
            return PipelineResult(
                status=404,
                body={"error": {"type": "not_found", "message": f"No route for model '{ctx.model_requested}'"}},
            )

        ctx.mode = route.mode

        # Build effective interceptor list (log + any per-route interceptors)
        interceptor_names = self._effective_interceptors(route)

        # Run request interceptors
        for iname in interceptor_names:
            interceptor = self._interceptors.get(iname)
            if interceptor and hasattr(interceptor, "on_request"):
                ctx = await interceptor.on_request(ctx)

        # Determine what to send upstream
        upstream_url, upstream_body = self._prepare_upstream(ctx)
        ctx.upstream_url = upstream_url
        ctx.upstream_body = upstream_body

        # Send to backend
        t0 = time.time()
        try:
            raw_resp = await self.sender.send(
                route.backend, upstream_url, upstream_body, headers=ctx.headers
            )
        except UpstreamError as e:
            resp = _upstream_error_context(e, (time.time() - t0) * 1000)
            # Log error
            for iname in interceptor_names:
                interceptor = self._interceptors.get(iname)
                if interceptor and hasattr(interceptor, "on_response"):
                    resp = await interceptor.on_response(ctx, resp)
            # Error body shape follows the CLIENT protocol (a2o / anthropic
            # passthrough clients need the Anthropic error envelope).
            is_anthropic_client = route.client_protocol == "anthropic"
            return PipelineResult(
                status=e.status,
                body=_upstream_error_payload(e, anthropic=is_anthropic_client),
            )

        # Convert response if needed
        if route.mode == ProxyMode.A2O:
            # Convert OpenAI response back to Anthropic
            client_resp = convert_response_o2a(
                raw_resp, model=route.effective_model, tool_name_map=ctx.tool_name_map
            )
        else:
            client_resp = raw_resp
            if route.dedup_reasoning and isinstance(client_resp, dict):
                _dedup_reasoning_payload(client_resp)

        # Build response context for interceptors
        resp = ResponseContext(
            status_code=200,
            parsed_body=client_resp,
            upstream_parsed_body=raw_resp,
            duration_ms=(time.time() - t0) * 1000,
        )

        # Run response interceptors
        for iname in interceptor_names:
            interceptor = self._interceptors.get(iname)
            if interceptor and hasattr(interceptor, "on_response"):
                resp = await interceptor.on_response(ctx, resp)

        return PipelineResult(status=200, body=resp.parsed_body or client_resp)

    async def _broadcast_chunk(
        self, ctx: RequestContext, names: list[str], sc: StreamChunk
    ) -> StreamChunk:
        """Run one client-facing SSE payload through the interceptor chain.

        The chain is a pipeline: each interceptor receives the previous one's
        (possibly rewritten) chunk, so registration order is significant when an
        interceptor rewrites. The registered interceptors (log, inject_extra_body)
        return the chunk unchanged, so order is currently irrelevant and the
        returned chunk equals the input. Callers may ignore the return value;
        the logger accumulates via its own per-request buffer keyed by
        request_id, not from the returned chunk."""
        for iname in names:
            interceptor = self._interceptors.get(iname)
            if interceptor and hasattr(interceptor, "on_stream_chunk"):
                sc = await interceptor.on_stream_chunk(ctx, sc)
        return sc

    async def _stream_a2o(
        self,
        ctx: RequestContext,
        route: RouteConfig,
        names: list[str],
        upstream_url: str,
        upstream_body: dict[str, Any],
        t0: float,
        sink: list[ResponseContext],
    ) -> AsyncIterator[bytes]:
        """A2O streaming: convert OpenAI chunks to Anthropic SSE. Yields client
        bytes and appends the resulting ResponseContext to ``sink`` so the caller
        can run on_stream_end uniformly. An UpstreamError is handled here (blocks
        already open on the wire are closed before the error event) rather than
        propagated, so the caller's on_stream_end still fires with the error resp."""
        converter = StreamingA2OConverter(
            model=route.effective_model, tool_name_map=ctx.tool_name_map
        )
        start_event = converter.start()
        yield start_event
        await self._broadcast_chunk(
            ctx, names, StreamChunk(client_raw_bytes=start_event, summarize=False)
        )

        try:
            async for chunk in self.sender.send_stream(
                route.backend, upstream_url, upstream_body, headers=ctx.headers
            ):
                events = converter.feed(chunk)
                for event in events:
                    yield event
                await self._broadcast_chunk(
                    ctx, names,
                    StreamChunk(client_raw_bytes=b"".join(events), upstream_parsed=chunk),
                )

            finish_events = converter.finish()
            for event in finish_events:
                yield event
            await self._broadcast_chunk(
                ctx, names,
                StreamChunk(client_raw_bytes=b"".join(finish_events), summarize=False),
            )
        except UpstreamError as e:
            # Close any blocks already opened on the wire before signalling the
            # error. The client has received message_start + deltas; an error
            # event without content_block_stop / message_stop leaves the Anthropic
            # SSE stream unterminated and breaks SDK parsers.
            for event in converter.finish():
                yield event
            yield _anthropic_error_event(e)
            sink.append(_upstream_error_context(e, (time.time() - t0) * 1000))
            return

        sink.append(ResponseContext(
            status_code=200,
            is_stream=True,
            parsed_body=converter.build_final_response(),
            duration_ms=(time.time() - t0) * 1000,
        ))

    async def _stream_passthrough(
        self,
        ctx: RequestContext,
        route: RouteConfig,
        names: list[str],
        upstream_url: str,
        upstream_body: dict[str, Any],
        t0: float,
        sink: list[ResponseContext],
    ) -> AsyncIterator[bytes]:
        """Same-protocol passthrough: relay upstream bytes verbatim (event names,
        comments, [DONE], provider-specific fields) while the logger parses a copy
        best-effort. Yields client bytes; appends the ResponseContext to ``sink``."""
        resp = ResponseContext(status_code=200, is_stream=True)
        # OpenAI-style backends may duplicate thinking under both `reasoning` and
        # `reasoning_content` on every chunk; rewrite to drop the duplicate before
        # both the client yield and the log broadcast (so the logger, which
        # concatenates all reasoning keys, doesn't double-count).
        #
        # The upstream sender yields raw HTTP transport blocks whose boundaries do
        # not align with SSE event boundaries, so we buffer and re-split on the
        # event separator (\n\n) before rewriting — otherwise an event straddling
        # two blocks fails to parse and its duplicate leaks through. The buffer is
        # only allocated when the flag is on; the passthrough fast path is unchanged.
        rewrite = route.dedup_reasoning
        buf = b""
        try:
            async for chunk in self.sender.send_stream_raw(
                route.backend, upstream_url, upstream_body, headers=ctx.headers
            ):
                if not rewrite:
                    yield chunk
                    await self._broadcast_chunk(ctx, names, StreamChunk(raw_bytes=chunk))
                    continue
                buf += chunk
                # Emit every complete event (terminated by \n\n); keep the tail.
                head, sep, buf = buf.rpartition(b"\n\n")
                if sep:
                    out = _rewrite_reasoning_sse(head + sep)
                    yield out
                    await self._broadcast_chunk(ctx, names, StreamChunk(raw_bytes=out))
            if rewrite and buf:
                out = _rewrite_reasoning_sse(buf)
                yield out
                await self._broadcast_chunk(ctx, names, StreamChunk(raw_bytes=out))
        except UpstreamError as e:
            # The error event goes to the *client*, so its wire format must follow
            # client_protocol (not backend_protocol).
            yield _anthropic_error_event(e) if route.client_protocol == "anthropic" else _openai_error_event(e)
            resp.error = str(e)
            resp.status_code = e.status
            resp.upstream_error_body = e.body or None

        resp.duration_ms = (time.time() - t0) * 1000
        sink.append(resp)

    async def handle_stream(
        self,
        ctx: RequestContext,
    ) -> AsyncIterator[bytes]:
        """Process a streaming request. Yields bytes to send to the client."""
        route = ctx.route
        if not route:
            yield json.dumps({"error": "No route found"}).encode()
            return

        ctx.mode = route.mode
        ctx.is_stream = True

        interceptor_names = self._effective_interceptors(route)

        # Track whether we reached normal stream end. If the client disconnects
        # mid-stream the generator is closed (GeneratorExit) before on_stream_end
        # runs; the finally then discards any per-request accumulator so it does
        # not leak in the interceptor's _stream_bufs for the proxy's lifetime.
        stream_ended = False
        try:
            for iname in interceptor_names:
                interceptor = self._interceptors.get(iname)
                if interceptor and hasattr(interceptor, "on_request"):
                    ctx = await interceptor.on_request(ctx)

            upstream_url, upstream_body = self._prepare_upstream(ctx)
            upstream_body["stream"] = True
            ctx.upstream_url = upstream_url
            ctx.upstream_body = upstream_body

            t0 = time.time()

            # Each mode's subgenerator yields client bytes and appends its final
            # ResponseContext here, so on_stream_end runs once below for every path
            # (including A2O upstream errors, which the subgenerator handles inline).
            sink: list[ResponseContext] = []
            if route.mode == ProxyMode.A2O:
                substream = self._stream_a2o(
                    ctx, route, interceptor_names, upstream_url, upstream_body, t0, sink
                )
            elif route.mode == ProxyMode.PASSTHROUGH:
                substream = self._stream_passthrough(
                    ctx, route, interceptor_names, upstream_url, upstream_body, t0, sink
                )
            else:
                substream = None

            if substream is not None:
                async for event in substream:
                    yield event
                resp = sink[0]
            else:
                # Unknown mode
                yield b"data: [DONE]\n\n"
                resp = ResponseContext(duration_ms=(time.time() - t0) * 1000)

            for iname in interceptor_names:
                interceptor = self._interceptors.get(iname)
                if interceptor and hasattr(interceptor, "on_stream_end"):
                    await interceptor.on_stream_end(ctx, resp)
            stream_ended = True
        finally:
            # Client disconnected (GeneratorExit) or any error left us before
            # on_stream_end: drop the per-request accumulator so it can't leak.
            if not stream_ended:
                for interceptor in self._interceptors.values():
                    discard = getattr(interceptor, "discard_stream", None)
                    if discard:
                        discard(ctx.request_id)

    def _effective_interceptors(self, route: RouteConfig) -> list[str]:
        """Build the effective interceptor chain for a route.

        Only the per-route interceptors (log, inject_extra_body). The proxy
        converts protocols, logs, and injects model-configured request params —
        it does not mutate model content or track sessions.
        """
        names: list[str] = []
        # Per-route interceptors
        for iname in route.interceptors:
            if iname not in names:
                names.append(iname)
        return names

    def _prepare_upstream(self, ctx: RequestContext) -> tuple[str, dict[str, Any]]:
        """Build the upstream URL and request body based on mode."""
        route = ctx.route
        assert route is not None

        base_url = route.backend.url

        if route.mode == ProxyMode.A2O:
            # Convert Anthropic request to OpenAI
            body = ctx.ensure_parsed()
            # Record truncated->original tool-name map so the response converter
            # can restore names the OpenAI 64-char limit forced us to shorten.
            ctx.tool_name_map = build_tool_name_mapping(body.get("tools"))
            upstream_body = convert_request_a2o(body)
            upstream_body["model"] = route.effective_model
            # Drop client-side cache_control on the a2o→OpenAI link: the gateway
            # injects its own breakpoints (already at Anthropic's 4-block cap), so
            # any the client still carries overflow to a 400. See _strip_cache_control.
            _strip_cache_control(upstream_body)
            # Apply extra_body
            if route.extra_body:
                upstream_body.update(route.extra_body)
            # Anthropic client headers (anthropic-version/-beta, x-api-key, ...)
            # would be forwarded verbatim to the OpenAI backend; strip them.
            ctx.headers = _scrub_anthropic_headers(ctx.headers)
            return _chat_completions_url(base_url), upstream_body

        elif route.mode == ProxyMode.PASSTHROUGH:
            # Same-protocol forwarding: the request path is preserved verbatim so
            # the proxy transparently relays *any* upstream endpoint (/v1/models,
            # /v1/embeddings, /health, ...), not just chat completions. Model
            # rewrite and extra_body injection are chat-completions-specific, so
            # they apply only on that endpoint and leave other endpoints untouched.
            body = ctx.ensure_parsed()
            is_inference = _is_inference_path(ctx.path, route.backend_protocol)

            if route.backend_protocol == "anthropic":
                if is_inference:
                    body["model"] = route.effective_model
                    # Inject the model's flattened params, identical to the a2o and
                    # OpenAI-passthrough branches (extra_body wins on key collision).
                    # The proxy injects what the model config declares; whether the
                    # backend accepts every key (e.g. min_p / chat_template_kwargs)
                    # is the backend's contract, not the proxy's to second-guess.
                    if route.extra_body:
                        body.update(route.extra_body)
                return _same_protocol_url(base_url, ctx.path), body

            # OpenAI-protocol passthrough.
            if is_inference:
                body["model"] = route.effective_model
                if route.extra_body:
                    body.update(route.extra_body)
                if route.reasoning_passback:
                    _inject_reasoning_passback(body)
            # OpenAI backend: drop any Anthropic-only headers the client sent.
            ctx.headers = _scrub_anthropic_headers(ctx.headers)
            return _same_protocol_url(base_url, ctx.path), body

        else:
            # Default: forward as-is
            body = ctx.ensure_parsed()
            return base_url.rstrip("/"), body

    async def close(self) -> None:
        """Shutdown: close clients and log files."""
        await self.sender.close()
        if self._log_interceptor is not None:
            self._log_interceptor.close()


class PipelineResult:
    """Result of a non-streaming pipeline execution."""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None):
        self.status = status
        self.body = body or {}
