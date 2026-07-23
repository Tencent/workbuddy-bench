"""JSONL logging interceptor -- records every request/response exchange."""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import Interceptor, RequestContext, ResponseContext, StreamChunk

log = logging.getLogger("proxy.interceptor.log")

_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking", "thinking_content")


class LogInterceptor:
    """Logs all exchanges to a JSONL file, compatible with the log viewer."""

    name = "log"

    def __init__(self, log_dir: str):
        self._log_dir = Path(log_dir) if log_dir else None
        self._seq = 0
        # Per-stream accumulators keyed by request_id
        self._stream_bufs: dict[str, _StreamAccumulator] = {}
        # Open log files keyed by instance_id ("" = shared file when a request
        # has no instance_id). Files are opened lazily per id so each trial's
        # requests land in <log_dir>/<instance_id>.jsonl, tying proxy logs back
        # to results/<trial>/config.json (same instance_id).
        self._log_files: dict[str, Any] = {}

        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _enabled(self) -> bool:
        return self._log_dir is not None

    def _file_for(self, instance_id: str):
        """Lazily open (and cache) the JSONL file for an instance_id."""
        if self._log_dir is None:
            return None
        key = instance_id or ""
        fh = self._log_files.get(key)
        if fh is None:
            name = f"{_safe_filename(instance_id)}.jsonl" if instance_id else "proxy_requests.jsonl"
            path = self._log_dir / name
            fh = open(path, "a", buffering=1, encoding="utf-8")
            self._log_files[key] = fh
            log.info("Logging to %s", path)
        return fh

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        """Mark request start time and snapshot the client body before mutation."""
        ctx.timestamp = time.time()
        if ctx.client_body is None:
            ctx.client_body = _json_safe(ctx.ensure_parsed())
        if ctx.is_stream:
            self._stream_bufs[ctx.request_id] = _StreamAccumulator()
        return ctx

    async def on_response(self, ctx: RequestContext, resp: ResponseContext) -> ResponseContext:
        """Log a non-streaming response."""
        if not self._enabled:
            return resp

        resp.duration_ms = (time.time() - ctx.timestamp) * 1000
        record = self._build_record(ctx, resp)
        self._write_record(record, _route_instance_id(ctx))
        return resp

    async def on_stream_chunk(self, ctx: RequestContext, chunk: StreamChunk) -> StreamChunk:
        """Accumulate streaming chunks for post-hoc logging."""
        buf = self._stream_bufs.get(ctx.request_id)
        if buf:
            buf.add_chunk(chunk)
        return chunk

    def discard_stream(self, request_id: str) -> None:
        """Drop a stream's accumulator without logging.

        Safe to call from a cancellation/finally path (no I/O, no await): ensures
        the per-request accumulator is freed even when the client disconnects
        mid-stream and on_stream_end never runs. Idempotent.
        """
        self._stream_bufs.pop(request_id, None)

    async def on_stream_end(self, ctx: RequestContext, resp: ResponseContext) -> None:
        """Write log record after stream completes."""
        if not self._enabled:
            return

        buf = self._stream_bufs.pop(ctx.request_id, None)
        resp.duration_ms = (time.time() - ctx.timestamp) * 1000

        if buf:
            resp.summary = buf.summarize(resp.parsed_body)
            resp.summary["status"] = resp.status_code

        record = self._build_record(ctx, resp, stream_summary=buf)
        self._write_record(record, _route_instance_id(ctx))

    def _build_record(
        self,
        ctx: RequestContext,
        resp: ResponseContext,
        stream_summary: "_StreamAccumulator | None" = None,
    ) -> dict[str, Any]:
        """Build a JSONL record."""
        self._seq += 1

        client_request_body = _json_safe(ctx.client_body or ctx.parsed_body or {})
        upstream_request_body = _json_safe(ctx.upstream_body) if ctx.upstream_body is not None else None

        # Extract messages from request for the log viewer
        body = client_request_body
        messages = body.get("messages")
        if messages is None and body.get("system"):
            # Anthropic format: build message list for viewer
            msgs = []
            sys = body.get("system", "")
            if sys:
                if isinstance(sys, str):
                    msgs.append({"role": "system", "content": sys})
                elif isinstance(sys, list):
                    text = "\n".join(
                        b.get("text", "") for b in sys if isinstance(b, dict)
                    )
                    msgs.append({"role": "system", "content": text})
            for m in body.get("messages", []):
                msgs.append(m)
            messages = msgs

        response_summary = resp.summary or self._response_summary(resp)
        if resp.parsed_body is not None and "body" not in response_summary:
            response_summary["body"] = _json_safe(resp.parsed_body)
        if resp.upstream_parsed_body is not None:
            response_summary["upstream_body"] = _json_safe(resp.upstream_parsed_body)
        # Stream summaries are built from _StreamAccumulator and already include
        # complete client/upstream SSE payloads. Keep this hook parameter only for
        # API clarity; no extra projection is needed here.
        if resp.upstream_error_body:
            response_summary["upstream_error_body"] = resp.upstream_error_body

        # Prefer the model the CLIENT requested. ctx.parsed_body["model"] is
        # rewritten in place to the backend model during _prepare_upstream, so read
        # the immutable client_body snapshot first; fall back to route slug.
        client_model = ""
        if isinstance(ctx.client_body, dict):
            client_model = str(ctx.client_body.get("model") or "")
        request_info: dict[str, Any] = {
            "method": ctx.method,
            "path": ctx.path,
            "model": client_model or ctx.model_requested or (ctx.route.slug if ctx.route else ""),
            "messages_count": len(messages) if messages else 0,
            "has_tools": bool(body.get("tools")),
            # Full client input body. Headers are intentionally excluded so
            # Authorization/API-key material such as ANTHROPIC_AUTH_TOKEN is not
            # written to proxy logs.
            "body": client_request_body,
        }
        if upstream_request_body is not None:
            request_info["upstream_url"] = ctx.upstream_url
            request_info["upstream_body"] = upstream_request_body

        record: dict[str, Any] = {
            "seq": self._seq,
            "id": ctx.request_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "route": ctx.route.slug if ctx.route else "unknown",
            "mode": ctx.mode.value,
            "client_protocol": ctx.route.client_protocol if ctx.route else "",
            "backend_protocol": ctx.route.backend_protocol if ctx.route else "",
            "stream": ctx.is_stream,
            "duration_ms": round(resp.duration_ms, 1),
            "request": request_info,
            "response": response_summary,
            "error": resp.error,
        }

        # Include messages for log viewer (truncated for large convos)
        if messages:
            record["messages"] = messages

        # Audit surface: the tool-usage / disallowed-tool audit filters records
        # by a top-level ``trial_id`` and needs the request ``tools[]`` schema.
        # Surface both here so an audit can reconstruct what the model was
        # offered and what it actually invoked (see the response ``tool_calls``
        # names below / in the stream summary).
        tools = body.get("tools")
        if tools:
            record["tools"] = tools

        # Metadata. trial_id comes from the bearer token's ``{trial_id}::{route}``
        # prefix (per-trial, set by the harness) and falls back to the route's
        # run-level instance_id when absent (legacy bare-token requests), so each
        # record still carries a key that ties it back to results/<trial>/config.json.
        trial_id = (ctx.meta.get("trial_id") if ctx.meta else "") or _route_instance_id(ctx)
        if trial_id:
            record["trial_id"] = trial_id
        if ctx.meta:
            record["meta"] = ctx.meta

        return record

    def _response_summary(self, resp: ResponseContext) -> dict[str, Any]:
        """Summarize a non-streaming response."""
        summary: dict[str, Any] = {"status": resp.status_code}
        if resp.parsed_body:
            # OpenAI format
            choices = resp.parsed_body.get("choices", [])
            if choices:
                choice = choices[0]
                summary["finish_reason"] = choice.get("finish_reason")
                msg = choice.get("message", {})
                content = msg.get("content", "")
                summary["content_len"] = len(content) if content else 0
                if isinstance(content, str):
                    summary["content"] = content
                reasoning = _extract_reasoning_text(msg)
                if reasoning:
                    summary["reasoning_len"] = len(reasoning)
                    summary["reasoning"] = reasoning
                tc = msg.get("tool_calls") or []
                summary["tool_calls_count"] = len(tc)
                if tc:
                    summary["tool_calls"] = [
                        _summarize_openai_tool_call(t)
                        for t in tc
                        if isinstance(t, dict)
                    ]
            usage = resp.parsed_body.get("usage")
            if usage:
                summary["usage"] = usage
            # Anthropic format
            if resp.parsed_body.get("type") == "message":
                blocks = resp.parsed_body.get("content", [])
                text = "".join(
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                reasoning = "".join(
                    str(b.get("thinking", ""))
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "thinking"
                )
                summary["content_len"] = len(text)
                if text:
                    summary["content"] = text
                if reasoning:
                    summary["reasoning_len"] = len(reasoning)
                    summary["reasoning"] = reasoning
                tool_uses = [
                    b for b in blocks
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                summary["tool_calls_count"] = len(tool_uses)
                if tool_uses:
                    summary["tool_calls"] = [_summarize_anthropic_tool_use(b) for b in tool_uses]
                summary["stop_reason"] = resp.parsed_body.get("stop_reason")
                summary["usage"] = resp.parsed_body.get("usage")
        return summary

    def _write_record(self, record: dict[str, Any], instance_id: str = "") -> None:
        """Write a single JSONL line to the file for this trial's instance_id."""
        fh = self._file_for(instance_id)
        if fh is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            fh.write(line)
            fh.flush()
        except Exception as e:
            log.warning("Failed to write log: %s", e)

    def close(self) -> None:
        for fh in self._log_files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._log_files.clear()


def _route_instance_id(ctx: RequestContext) -> str:
    """The route's instance_id for this request (empty when not set)."""
    route = getattr(ctx, "route", None)
    return getattr(route, "instance_id", "") or "" if route else ""


# Filesystem-safe form of an instance_id for use as a log filename. instance_ids
# are alphanumeric + dashes today, but model_slug-derived ids can contain "/" and
# ":"; replace anything outside [A-Za-z0-9._-] so the path stays single-segment.
def _safe_filename(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)


def _json_safe(value: Any) -> Any:
    return copy.deepcopy(value)


def _extract_reasoning_text(obj: dict[str, Any]) -> str:
    """Return provider-specific reasoning text from a message or delta."""
    parts: list[str] = []
    for key in _REASONING_KEYS:
        value = obj.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    nested = _extract_reasoning_text(item)
                    if nested:
                        parts.append(nested)
    return "".join(parts)


def _summarize_openai_tool_call(tc: dict[str, Any]) -> dict[str, Any]:
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    arguments = fn.get("arguments", "")
    out: dict[str, Any] = {
        "id": tc.get("id"),
        "type": tc.get("type"),
        "name": fn.get("name"),
        "arguments": arguments,
        "arguments_len": len(arguments) if isinstance(arguments, str) else 0,
    }
    return {k: v for k, v in out.items() if v is not None}


def _summarize_anthropic_tool_use(block: dict[str, Any]) -> dict[str, Any]:
    input_data = block.get("input")
    arguments = json.dumps(input_data, ensure_ascii=False) if input_data is not None else ""
    out: dict[str, Any] = {
        "id": block.get("id"),
        "type": "tool_use",
        "name": block.get("name"),
        "input": input_data,
        "arguments": arguments,
        "arguments_len": len(arguments),
    }
    return {k: v for k, v in out.items() if v is not None}


def _summarize_stream_tool_call(tc: dict[str, Any]) -> dict[str, Any]:
    arguments = tc.get("arguments", "")
    out: dict[str, Any] = {
        "index": tc.get("index"),
        "id": tc.get("id"),
        "type": tc.get("type"),
        "name": tc.get("name"),
        "input": tc.get("input"),
        "arguments": arguments,
        "arguments_len": len(arguments) if isinstance(arguments, str) else 0,
    }
    return {k: v for k, v in out.items() if v is not None}


# Slot base for tool_call deltas/blocks that arrive without an explicit index,
# so they never collide with real (small) indices in the accumulator dict.
_MISSING_INDEX_BASE = 1_000_000


def _int_or_default(value: Any, default: int) -> int:
    return value if isinstance(value, int) else default


def _parse_sse_json_payloads(raw: bytes) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in _iter_sse_payloads(raw.decode("utf-8", errors="replace")):
        if event == "[DONE]":
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


# Cap on the partial-event carry-over buffer. A well-formed SSE event is far
# smaller; if no blank-line boundary appears within this many bytes the stream is
# malformed (or a single pathological event), so we stop buffering and let the
# accumulated bytes be parsed best-effort instead of growing without bound.
_MAX_SSE_LEFTOVER = 1 << 20  # 1 MiB


def _frame_sse_events(leftover: bytes, new: bytes) -> tuple[bytes, bytes]:
    """Split a running SSE byte stream into complete events plus a remainder.

    Events are blank-line (``\\n\\n``) delimited. Returns ``(complete, tail)``
    where ``complete`` ends on an event boundary (safe to parse) and ``tail`` is
    the trailing partial event to carry into the next chunk. This makes per-chunk
    parsing robust to network framing that cuts across event boundaries.

    Bounded: if no boundary is found and the buffer exceeds ``_MAX_SSE_LEFTOVER``,
    the whole buffer is returned as ``complete`` (parsed best-effort, then reset)
    so an unterminated stream cannot grow the carry-over buffer indefinitely.
    """
    buf = leftover + new
    idx = buf.rfind(b"\n\n")
    if idx == -1:
        if len(buf) > _MAX_SSE_LEFTOVER:
            return buf, b""
        return b"", buf
    return buf[: idx + 2], buf[idx + 2 :]


def _iter_sse_payloads(text: str) -> list[str]:
    payloads: list[str] = []
    event_lines: list[str] = []
    for line in text.splitlines():
        if line == "":
            payload = _payload_from_sse_event(event_lines)
            if payload is not None:
                payloads.append(payload)
            event_lines = []
            continue
        event_lines.append(line)
    payload = _payload_from_sse_event(event_lines)
    if payload is not None:
        payloads.append(payload)
    return payloads


def _payload_from_sse_event(lines: list[str]) -> str | None:
    data_lines = [line[5:].lstrip() for line in lines if line.startswith("data:")]
    if not data_lines:
        return None
    return "\n".join(data_lines).strip()


class _StreamAccumulator:
    """Collect complete streaming input/output plus compact analysis fields."""

    def __init__(self):
        # Only a running byte count is kept (for the ``raw_bytes`` size field);
        # raw chunk bytes and per-event dicts are no longer retained — see
        # summarize() for why the reconstructed body is the sole stream artifact.
        self.client_bytes = 0
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.stop_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        # Carry-over for SSE events split across network chunks: a chunk boundary
        # is not an event boundary, so we hold the trailing incomplete event and
        # prepend it to the next chunk before parsing. Without this, a usage-
        # bearing event (e.g. Anthropic message_delta) straddling two chunks fails
        # to parse on both halves and is silently dropped from the summary.
        self._client_sse_leftover = b""
        self._upstream_sse_leftover = b""

    def add_chunk(self, chunk: StreamChunk) -> None:
        """Record one streaming step.

        Client fields are what the harness received. Upstream fields are what the
        model backend returned before any protocol conversion. Legacy callers that
        only set ``raw_bytes``/``parsed`` are treated as same-protocol traffic and
        populate both sides.
        """
        legacy_raw = chunk.raw_bytes and not chunk.client_raw_bytes and not chunk.upstream_raw_bytes
        client_raw = chunk.client_raw_bytes or chunk.raw_bytes
        upstream_raw = chunk.upstream_raw_bytes or (chunk.raw_bytes if legacy_raw else b"")
        client_parsed = chunk.client_parsed
        upstream_parsed = chunk.upstream_parsed

        if chunk.parsed is not None:
            # Legacy same-protocol path: this payload is both upstream and client.
            client_parsed = client_parsed or chunk.parsed
            upstream_parsed = upstream_parsed or chunk.parsed

        if client_raw:
            self.client_bytes += len(client_raw)
            if client_parsed is None:
                # Frame across chunk boundaries before parsing (carry the partial
                # trailing event to the next chunk).
                complete, self._client_sse_leftover = _frame_sse_events(
                    self._client_sse_leftover, client_raw
                )
                for payload in _parse_sse_json_payloads(complete):
                    if (
                        chunk.summarize
                        and chunk.summary_parsed is None
                        and upstream_parsed is None
                        and not upstream_raw
                    ):
                        self._process_payload_for_summary(payload)

        if upstream_raw:
            if upstream_parsed is None:
                complete, self._upstream_sse_leftover = _frame_sse_events(
                    self._upstream_sse_leftover, upstream_raw
                )
                for payload in _parse_sse_json_payloads(complete):
                    if chunk.summarize and chunk.summary_parsed is None:
                        self._process_payload_for_summary(payload)

        if chunk.summarize:
            # Prefer an explicitly chosen summary payload, then upstream model
            # payloads because they preserve provider-specific fields such as
            # reasoning_content.
            summary_payload = chunk.summary_parsed or upstream_parsed or client_parsed
            if isinstance(summary_payload, dict):
                self._process_payload_for_summary(summary_payload)

    def _process_payload_for_summary(self, obj: dict) -> None:
        """Extract compact analysis data from a parsed SSE payload."""
        self.usage = obj.get("usage") or self.usage

        # Anthropic streaming event format.
        event_type = obj.get("type")
        if event_type:
            self._process_anthropic_event(obj)

        # OpenAI chat-completions stream format. ``or []`` guards against
        # present-but-null choices / tool_calls fields (some backends emit
        # explicit nulls).
        for choice in (obj.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                continue
            self._process_openai_delta(delta)

    def _process_openai_delta(self, delta: dict[str, Any]) -> None:
        content = delta.get("content")
        if isinstance(content, str):
            self.content_parts.append(content)

        reasoning = _extract_reasoning_text(delta)
        if reasoning:
            self.reasoning_parts.append(reasoning)

        for tc in (delta.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            idx = _int_or_default(tc.get("index"), _MISSING_INDEX_BASE + len(self.tool_calls))
            bucket = self.tool_calls.setdefault(
                idx,
                {"index": idx, "id": None, "type": "function", "name": None, "arguments": ""},
            )
            if tc.get("id"):
                bucket["id"] = tc["id"]
            if tc.get("type"):
                bucket["type"] = tc["type"]
            fn = tc.get("function", {})
            if not isinstance(fn, dict):
                continue
            if fn.get("name"):
                bucket["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                bucket["arguments"] += fn["arguments"]

    def _process_anthropic_event(self, obj: dict[str, Any]) -> None:
        event_type = obj.get("type")
        if event_type == "message_start":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            usage = message.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            return

        if event_type == "message_delta":
            delta = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
            if delta.get("stop_reason"):
                self.stop_reason = str(delta["stop_reason"])
            usage = obj.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            return

        if event_type == "content_block_start":
            block = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
            btype = block.get("type")
            if btype == "tool_use":
                idx = _int_or_default(obj.get("index"), _MISSING_INDEX_BASE + len(self.tool_calls))
                self.tool_calls[idx] = {
                    "index": idx,
                    "id": block.get("id"),
                    "type": "tool_use",
                    "name": block.get("name"),
                    "input": block.get("input"),
                    # Anthropic streaming normally starts with input={} and then
                    # sends the actual JSON via input_json_delta. Do not seed
                    # arguments with "{}" or the reconstructed payload is corrupt.
                    "arguments": "",
                }
            elif btype == "text" and isinstance(block.get("text"), str):
                self.content_parts.append(block["text"])
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                self.reasoning_parts.append(block["thinking"])
            return

        if event_type != "content_block_delta":
            return

        delta = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
        dtype = delta.get("type")
        if dtype == "text_delta" and isinstance(delta.get("text"), str):
            self.content_parts.append(delta["text"])
        elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str):
            self.reasoning_parts.append(delta["thinking"])
        elif dtype == "input_json_delta" and isinstance(delta.get("partial_json"), str):
            idx = _int_or_default(obj.get("index"), _MISSING_INDEX_BASE + len(self.tool_calls))
            bucket = self.tool_calls.setdefault(
                idx,
                {"index": idx, "id": None, "type": "tool_use", "name": None, "arguments": ""},
            )
            bucket["arguments"] += delta["partial_json"]

    def summarize(self, final_client_body: dict[str, Any] | None = None) -> dict[str, Any]:
        # Flush any trailing event with no closing blank line (some backends omit
        # the final \n\n). Both leftovers are empty when chunks arrived pre-parsed.
        # In same-protocol passthrough the SAME raw bytes populate both client and
        # upstream sides (legacy_raw), so the two leftovers are identical — flush
        # only the distinct ones to avoid double-counting the final event (which
        # would duplicate trailing text / corrupt trailing tool-call JSON).
        flushed: list[bytes] = []
        for leftover in (self._upstream_sse_leftover, self._client_sse_leftover):
            if leftover.strip() and leftover not in flushed:
                flushed.append(leftover)
                for payload in _parse_sse_json_payloads(leftover):
                    self._process_payload_for_summary(payload)
        self._upstream_sse_leftover = b""
        self._client_sse_leftover = b""

        content = "".join(self.content_parts)
        reasoning = "".join(self.reasoning_parts)
        tool_calls = [self.tool_calls[idx] for idx in sorted(self.tool_calls)]
        summary: dict[str, Any] = {
            "status": 200,
            "streamed": True,
            "finish_reason": self.finish_reason,
            "content_len": len(content),
            "tool_calls_count": len(tool_calls),
            "raw_bytes": self.client_bytes,
        }
        if self.stop_reason:
            summary["stop_reason"] = self.stop_reason
        if content:
            summary["content"] = content
        if reasoning:
            summary["reasoning_len"] = len(reasoning)
            summary["reasoning"] = reasoning
        if self.usage:
            summary["usage"] = self.usage
        if tool_calls:
            summary["tool_calls"] = [_summarize_stream_tool_call(tc) for tc in tool_calls]

        # Streaming logs retain the reconstructed *non-streaming* response only:
        # the full assembled body plus the compact analysis fields above
        # (content / reasoning / tool_calls / usage / stop_reason). The verbose
        # per-event lists and raw SSE blobs (events / client_events /
        # upstream_events / raw_sse / *_raw_sse) are intentionally NOT emitted —
        # on long conversations they dominate the JSONL size and memory while
        # adding nothing the reconstructed body doesn't already capture.
        if final_client_body is not None:
            summary["body"] = _json_safe(final_client_body)
        return summary
