"""Interceptor base classes and context objects."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import RouteConfig, ProxyMode


@dataclass
class RequestContext:
    """All information about an incoming request, flowing through the pipeline."""
    # Identity
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:16]}")
    timestamp: float = field(default_factory=time.time)

    # Client request
    method: str = "POST"
    path: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)

    # Route resolution
    route: RouteConfig | None = None
    mode: ProxyMode = ProxyMode.PASSTHROUGH

    # Parsed request body (None for pure passthrough until an interceptor needs it).
    # client_body is the immutable body received from the harness; parsed_body may
    # be mutated while preparing the upstream request (model rewrite, extra_body).
    raw_body: bytes = b""
    parsed_body: dict[str, Any] | None = None
    client_body: dict[str, Any] | None = None
    is_stream: bool = False

    # Bench context metadata
    meta: dict[str, str] = field(default_factory=dict)

    # For protocol mapping: the transformed request body to send upstream
    upstream_body: dict[str, Any] | None = None
    upstream_url: str = ""
    upstream_headers: dict[str, str] = field(default_factory=dict)

    # A2O only: maps truncated tool names (sent to the OpenAI backend) back to
    # the original Anthropic names, so the response converter can restore them.
    tool_name_map: dict[str, str] = field(default_factory=dict)

    def ensure_parsed(self) -> dict[str, Any]:
        """Lazily parse body JSON. Returns the parsed dict."""
        if self.parsed_body is None:
            import json
            self.parsed_body = json.loads(self.raw_body) if self.raw_body else {}
        return self.parsed_body

    @property
    def model_requested(self) -> str:
        """The model name the client requested."""
        if self.parsed_body:
            return self.parsed_body.get("model", "")
        return ""


@dataclass
class StreamChunk:
    """A single chunk in a streaming response.

    ``raw_bytes`` / ``parsed`` are legacy aliases for same-protocol streams.
    New call sites should use client_* for bytes sent to the harness and
    upstream_* for bytes/payloads received from the model backend, so the logger
    can preserve both sides of protocol-converted exchanges.
    """
    raw_bytes: bytes = b""          # Legacy raw bytes (same as client bytes)
    parsed: dict[str, Any] | None = None  # Legacy parsed payload (same-protocol)
    is_done: bool = False           # True for the terminal [DONE] signal
    client_raw_bytes: bytes = b""
    client_parsed: dict[str, Any] | None = None
    upstream_raw_bytes: bytes = b""
    upstream_parsed: dict[str, Any] | None = None
    summary_parsed: dict[str, Any] | None = None
    summarize: bool = True          # False for client-only converted SSE events


@dataclass
class ResponseContext:
    """The upstream response, either complete or streaming."""
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    # Non-streaming: full response body returned to the client
    body: bytes = b""
    parsed_body: dict[str, Any] | None = None

    # Raw upstream response before protocol conversion, when available.
    upstream_parsed_body: dict[str, Any] | None = None

    # Streaming
    is_stream: bool = False

    # Post-processing data (filled after response completes)
    duration_ms: float = 0.0
    error: str | None = None
    upstream_error_body: str | None = None

    # Collected summary for logging (filled by log interceptor)
    summary: dict[str, Any] = field(default_factory=dict)

    def ensure_parsed(self) -> dict[str, Any]:
        """Lazily parse response body JSON."""
        if self.parsed_body is None:
            import json
            self.parsed_body = json.loads(self.body) if self.body else {}
        return self.parsed_body


@runtime_checkable
class Interceptor(Protocol):
    """Composable pipeline hook."""

    @property
    def name(self) -> str:
        """Interceptor identifier for debugging."""
        ...

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        """Process/modify the request before it goes upstream."""
        ...

    async def on_response(self, ctx: RequestContext, resp: ResponseContext) -> ResponseContext:
        """Process/modify the non-streaming response before returning to client."""
        ...

    async def on_stream_chunk(self, ctx: RequestContext, chunk: StreamChunk) -> StreamChunk:
        """Process a streaming chunk. Called for each chunk."""
        ...

    async def on_stream_end(self, ctx: RequestContext, resp: ResponseContext) -> None:
        """Called when a streaming response completes. Good for post-hoc logging."""
        ...
