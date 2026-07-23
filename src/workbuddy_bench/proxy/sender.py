"""Upstream HTTP sender -- stream-aware request forwarding with retries."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import time
from typing import Any, AsyncIterator

import httpx

from .config import BackendConfig

log = logging.getLogger("proxy.sender")

MAX_UPSTREAM_ERROR_BODY = 4000

# Transport-level failures worth retrying: timeouts (connect/read/write/pool),
# network errors (connect/read/write/close), and protocol errors
# (incl. RemoteProtocolError). All subclass httpx.TransportError.
_RETRYABLE_EXC = httpx.TransportError

# httpx connect/write timeouts (read/pool follow the backend's own timeout).
_CONNECT_TIMEOUT_S = 30.0
_WRITE_TIMEOUT_S = 30.0
# Keepalive pool size when a backend has no explicit max_concurrent cap.
_DEFAULT_KEEPALIVE = 64


def _retry_delay(base_delay: float) -> float:
    """Fixed-interval retry delay (not exponential backoff), plus small jitter.

    Why fixed, not exponential:
      - Exponential backoff exists to disperse a thundering herd — many
        independent clients hammering a shared resource they are overwhelming.
        That premise does not hold here: concurrency is bounded upstream by the
        eval orchestration (container/job limits), and backend 5xx are upstream
        outages (the TI gateway is down of its own accord), not congestion we are
        causing. Backing off longer does not help the backend recover, so the
        exponential curve buys nothing here.
      - Exponential's tail is actively harmful: base=30s, 1.5**attempt over 10
        tries reaches ~19 min for a single sleep and ~57 min cumulative. That far
        exceeds cbc's client-side stream/first-token timeout (600s). When the
        proxy's retry window outlasts that, cbc times out and starts its own
        retry while the proxy is still retrying — nested retries that multiply
        into ~25-min dead trials with empty patches (the 2026-07-03 incident).

    Fixed interval keeps the whole proxy retry window = max_retries * base_delay
    small and bounded (default 6 * 5s = 30s), safely inside cbc's 600s so the two
    layers never overlap. The proxy absorbs short upstream blips cheaply and
    transparently; anything longer fails fast up to cbc, which owns the expensive
    retry. Jitter (+0..20%) desynchronizes concurrent streams so they don't
    re-hit the backend in a synchronized pulse every interval.
    """
    return base_delay + random.uniform(0.0, base_delay * 0.2)


class UpstreamSender:
    """Manages backend HTTP clients and sends requests with retry logic."""

    def __init__(
        self,
        default_timeout: float = 600.0,
        verify_tls: bool = False,
    ):
        # Concurrency is throttled per model (per backend), not globally: each
        # backend with a configured max_concurrent gets its own semaphore, keyed
        # the same way as its httpx client. A backend without max_concurrent is
        # unlimited (concurrency is then bounded upstream by the eval job's
        # container/trial limits — the proxy adds no cap). There is deliberately
        # no global semaphore, so a busy model can't starve others sharing the
        # proxy.
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._default_timeout = default_timeout
        self._verify_tls = verify_tls
        self._active = 0

    @staticmethod
    def _backend_key(backend: BackendConfig) -> str:
        # Key on URL + a digest of the full key so backends that share a URL but
        # differ only past the first few key chars don't collide.
        key_digest = hashlib.sha256(backend.key.encode()).hexdigest()[:12] if backend.key else ""
        return f"{backend.url}|{key_digest}"

    def _get_semaphore(self, backend: BackendConfig) -> asyncio.Semaphore | None:
        """Per-backend concurrency limiter, or None when this model is unlimited."""
        if not backend.max_concurrent or backend.max_concurrent <= 0:
            return None
        key = self._backend_key(backend)
        sem = self._semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(backend.max_concurrent)
            self._semaphores[key] = sem
        return sem

    def _get_client(self, backend: BackendConfig) -> httpx.AsyncClient:
        """Get or create an httpx client for a backend."""
        key = self._backend_key(backend)
        if key not in self._clients:
            timeout = httpx.Timeout(
                connect=_CONNECT_TIMEOUT_S,
                read=backend.timeout or self._default_timeout,
                write=_WRITE_TIMEOUT_S,
                pool=backend.timeout or self._default_timeout,
            )
            # Connection pool must never be a tighter bound than the (per-model)
            # semaphore, or requests would queue on connections before the
            # limiter. Leave total connections unbounded; size keepalive to the
            # model's own cap when set, else a generous default.
            keepalive = backend.max_concurrent if backend.max_concurrent else _DEFAULT_KEEPALIVE
            self._clients[key] = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=None,
                    max_keepalive_connections=keepalive,
                ),
                verify=self._verify_tls,
            )
        return self._clients[key]

    def _build_headers(
        self,
        backend: BackendConfig,
        client_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build upstream headers, passing the client's through by default.

        Dropped: host/content-length (transport framing httpx recomputes after
        the proxy rewrites the body) and authorization/x-api-key (the client
        sends a placeholder token; the real backend key is injected below).

        Then any configured ``backend.headers`` (model.backend_headers) are
        merged on top of the passthrough headers, EXCEPT reserved keys (the
        _DROP set) which are skipped so a misconfigured header can't break auth
        or transport framing. The backend key's Authorization is injected last
        and always wins.
        """
        # ``content-type`` is dropped from the passthrough set (not just
        # defaulted) because setdefault is case-sensitive: a client sending
        # lowercase ``content-type`` would otherwise survive AND get a second,
        # capitalized ``Content-Type`` added below — two Content-Type headers,
        # which strict backends (e.g. the SN5 router) reject with 502. Strip any
        # client-sent Content-Type here and set exactly one canonical value.
        _DROP = ("host", "content-length", "authorization", "x-api-key", "content-type")
        if client_headers is None:
            headers: dict[str, str] = {}
        else:
            headers = {
                k: v
                for k, v in client_headers.items()
                if k.lower() not in _DROP
            }
        for k, v in backend.headers.items():
            if k.lower() in _DROP:
                continue
            headers[k] = v
        headers["Content-Type"] = "application/json"
        if backend.key:
            headers["Authorization"] = f"Bearer {backend.key}"
        return headers

    async def send(
        self,
        backend: BackendConfig,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send a non-streaming request to the backend. Returns parsed JSON."""
        client = self._get_client(backend)
        limiter = self._get_semaphore(backend)  # None = unlimited for this model
        headers = self._build_headers(backend, headers)
        max_retries = backend.max_retries
        base_delay = backend.retry_delay

        for attempt in range(max_retries + 1):
            log.info("-> POST %s attempt=%d/%d (active=%d)",
                     url, attempt + 1, max_retries + 1, self._active)
            try:
                async with (limiter or contextlib.nullcontext()):
                    self._active += 1
                    try:
                        resp = await client.post(url, json=body, headers=headers)
                    finally:
                        self._active -= 1

                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise UpstreamError(
                            f"Backend returned 200 with non-JSON body: {exc}",
                            status=502,
                            body=resp.text[:MAX_UPSTREAM_ERROR_BODY],
                        )

                if resp.status_code in (429, 502, 503, 504) and attempt < max_retries:
                    delay = _retry_delay(base_delay)
                    log.warning("Backend %d, retrying in %.1fs (fixed, attempt %d/%d)...",
                                resp.status_code, delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue

                raise UpstreamError(
                    f"Backend returned {resp.status_code}",
                    status=resp.status_code,
                    body=resp.text[:MAX_UPSTREAM_ERROR_BODY],
                )

            except _RETRYABLE_EXC as exc:
                if attempt < max_retries:
                    delay = _retry_delay(base_delay)
                    log.warning("%s, retrying in %.1fs (fixed, attempt %d/%d)...",
                                type(exc).__name__, delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue
                raise UpstreamError(f"{type(exc).__name__} after {max_retries + 1} attempts: {exc}")

        raise UpstreamError("Max retries exhausted")

    async def send_stream(
        self,
        backend: BackendConfig,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a streaming request. Yields parsed SSE data payloads.

        The body should already have stream=True set.
        """
        async for payload in self._send_stream_impl(backend, url, body, raw=False, headers=headers):
            yield payload

    async def send_stream_raw(
        self,
        backend: BackendConfig,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[bytes]:
        """Send a streaming request and yield upstream bytes unchanged."""
        async for payload in self._send_stream_impl(backend, url, body, raw=True, headers=headers):
            yield payload

    async def _send_stream_impl(
        self,
        backend: BackendConfig,
        url: str,
        body: dict[str, Any],
        *,
        raw: bool,
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[Any]:
        client = self._get_client(backend)
        limiter = self._get_semaphore(backend)  # None = unlimited for this model
        headers = self._build_headers(backend, headers)
        max_retries = backend.max_retries
        base_delay = backend.retry_delay

        for attempt in range(max_retries + 1):
            log.info("-> STREAM %s attempt=%d/%d (active=%d)",
                     url, attempt + 1, max_retries + 1, self._active)
            retry_delay = None
            # Once we have yielded any 200-stream payload to the caller, a later
            # transport error cannot be retried by replaying the POST — doing so
            # would re-yield the already-sent chunks and duplicate the stream.
            # Track this so such errors fail hard instead of looping.
            yielded = False
            try:
                async with (limiter or contextlib.nullcontext()):
                    self._active += 1
                    try:
                        async with client.stream("POST", url, json=body, headers=headers) as resp:
                            if resp.status_code == 200:
                                if raw:
                                    async for chunk in resp.aiter_bytes():
                                        yielded = True
                                        yield chunk
                                    return
                                async for payload in self._parse_sse(resp):
                                    yielded = True
                                    yield payload
                                # A 200 SSE stream that yielded ZERO data payloads is
                                # a degenerate/truncated response (clean EOF before any
                                # data, or before the [DONE] sentinel). Treat it as an
                                # error so the caller surfaces a failure instead of a
                                # silently-empty "successful" message. (Only reachable
                                # on a clean EOF; abnormal drops raise TransportError
                                # and are handled by the retry path above.)
                                if not yielded:
                                    raise UpstreamError(
                                        "Backend returned 200 with an empty SSE stream "
                                        "(no data events)",
                                        status=502,
                                    )
                                return

                            if resp.status_code in (429, 502, 503, 504) and attempt < max_retries:
                                # Defer the delay until AFTER releasing the
                                # concurrency slot (matches send()); sleeping
                                # inside the semaphore would hold a slot idle.
                                retry_delay = _retry_delay(base_delay)
                                log.warning("Backend %d, retrying in %.1fs (fixed, attempt %d/%d)...",
                                            resp.status_code, retry_delay, attempt + 1, max_retries)
                            else:
                                text = (await resp.aread()).decode("utf-8", errors="replace")
                                raise UpstreamError(
                                    f"Backend returned {resp.status_code}",
                                    status=resp.status_code,
                                    body=text[:MAX_UPSTREAM_ERROR_BODY],
                                )
                    finally:
                        self._active -= 1

            except _RETRYABLE_EXC as exc:
                if yielded:
                    # Mid-stream failure after partial output: cannot replay.
                    raise UpstreamError(
                        f"{type(exc).__name__} mid-stream after partial output: {exc}"
                    )
                if attempt < max_retries:
                    delay = _retry_delay(base_delay)
                    log.warning("%s, retrying in %.1fs (fixed, attempt %d/%d)...",
                                type(exc).__name__, delay, attempt + 1, max_retries)
                    await asyncio.sleep(delay)
                    continue
                raise UpstreamError(f"{type(exc).__name__} after {max_retries + 1} attempts: {exc}")

            if retry_delay is not None:
                await asyncio.sleep(retry_delay)
                continue

    @staticmethod
    async def _parse_sse(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        """Parse SSE stream into JSON payloads."""
        buffer: list[str] = []

        async for line in resp.aiter_lines():
            if line == "":
                # Empty line = end of event
                data_lines = [l[5:].lstrip() for l in buffer if l.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                payload = "\n".join(data_lines).strip()
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("Malformed SSE: %s", payload[:200])
                continue

            buffer.append(line)

        # Handle trailing data without final empty line
        if buffer:
            data_lines = [l[5:].lstrip() for l in buffer if l.startswith("data:")]
            payload = "\n".join(data_lines).strip()
            if payload and payload != "[DONE]":
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass

    @property
    def active_requests(self) -> int:
        return self._active

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


class UpstreamError(Exception):
    """Error from upstream backend."""
    def __init__(self, message: str, status: int = 502, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
