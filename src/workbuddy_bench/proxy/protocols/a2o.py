"""Anthropic Messages -> OpenAI Chat Completions protocol conversion.

Handles both request conversion (A->O) and response conversion (O->A),
including streaming responses. The proxy only converts protocols; it does not
alter model content (no tool-call truncation, no max_tokens override).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

log = logging.getLogger("proxy.protocols.a2o")

# OpenAI enforces a 64-char limit on function/tool names; Anthropic does not.
# Claude Code's MCP tool names (mcp__server__tool) routinely exceed this and
# trigger 400s on validating backends. Truncate to "{prefix}_{hash}" so the
# name stays unique, and keep a mapping to restore the original on the response.
_OPENAI_MAX_TOOL_NAME_LENGTH = 64
_TOOL_NAME_HASH_LENGTH = 8
_TOOL_NAME_PREFIX_LENGTH = _OPENAI_MAX_TOOL_NAME_LENGTH - _TOOL_NAME_HASH_LENGTH - 1  # 55


def truncate_tool_name(name: str) -> str:
    """Truncate a tool name to OpenAI's 64-char limit, deterministically.

    Names already within the limit are returned unchanged. Longer names become
    ``{55-char-prefix}_{8-char-sha256}`` so distinct long names don't collide.
    """
    if len(name) <= _OPENAI_MAX_TOOL_NAME_LENGTH:
        return name
    name_hash = hashlib.sha256(name.encode()).hexdigest()[:_TOOL_NAME_HASH_LENGTH]
    return f"{name[:_TOOL_NAME_PREFIX_LENGTH]}_{name_hash}"


def build_tool_name_mapping(anthro_tools: list | None) -> dict[str, str]:
    """Map truncated tool names back to originals (only for truncated tools)."""
    mapping: dict[str, str] = {}
    for tool in anthro_tools or []:
        if not isinstance(tool, dict):
            continue
        original = tool.get("name", "")
        truncated = truncate_tool_name(original)
        if truncated != original:
            mapping[truncated] = original
    return mapping


_DEFAULT_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}
_ALLOWED_SCHEMA_KEYS = {
    "type",
    "description",
    "properties",
    "required",
    "items",
    "enum",
    # Purely advisory keywords that most OpenAI-compatible backends tolerate and
    # that help the model use a tool correctly. Kept on purpose. Stricter
    # constraint keywords (minimum/maximum/pattern/minLength/...) stay dropped
    # because those are what tend to trigger 400s on validating backends.
    "default",
    "format",
}
_SCHEMA_UNION_KEYS = ("anyOf", "oneOf", "allOf")


# ---------------------------------------------------------------------------
# Request: Anthropic -> OpenAI
# ---------------------------------------------------------------------------

# Anthropic request keys the proxy translates explicitly (shape differs from
# OpenAI). Everything not listed here is copied through verbatim: that preserves
# top_k, metadata, max_tokens, seed, and any future/provider-specific param the
# backend understands, instead of silently dropping it via a whitelist.
_TRANSLATED_ANTHROPIC_KEYS = frozenset({
    "system",
    "messages",
    "tools",
    "tool_choice",
    "stop_sequences",
    "thinking",
    "metadata",  # translated to top-level ``user`` below; not passed through
    "stream",  # rebuilt below; pipeline also force-sets stream=True for streams
})


def convert_request_a2o(anthro_body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Messages request to OpenAI Chat Completions format.

    Returns the OpenAI request body dict. Known Anthropic-shaped fields are
    translated; all other top-level params are passed through unchanged so the
    backend still receives top_k, metadata, max_tokens, seed, etc.
    """
    system_messages = []
    non_system_messages = []

    # System prompt
    system = anthro_body.get("system")
    if system:
        sys_msg = _convert_system(system)
        if sys_msg:
            system_messages.append(sys_msg)

    # Messages. Some Anthropic clients surface system reminders as messages with
    # role=system; OpenAI-compatible backends commonly require every system
    # message to appear before the first user/assistant/tool message.
    for msg in anthro_body.get("messages", []):
        for converted in _convert_message(msg):
            if converted.get("role") == "system":
                system_messages.append(converted)
            else:
                non_system_messages.append(converted)

    oai_messages = _merge_system_messages(system_messages) + non_system_messages

    # Tools
    oai_tools = _convert_tools(anthro_body.get("tools"))

    # Pass through any param we don't explicitly translate (top_k, metadata,
    # max_tokens, seed, ...). stop_sequences is translated to ``stop`` below;
    # max_tokens is NOT injected with a default — if the client omitted it we
    # leave it out and let the backend apply its own default (no silent cap).
    body: dict[str, Any] = {
        k: v for k, v in anthro_body.items() if k not in _TRANSLATED_ANTHROPIC_KEYS
    }
    body["model"] = anthro_body.get("model", "")
    body["messages"] = oai_messages
    is_stream = bool(anthro_body.get("stream", False))
    body["stream"] = is_stream
    # Ask OpenAI-compatible backends (e.g. vLLM) to emit a final usage chunk in
    # streaming mode; otherwise the converted Anthropic message reports 0 output
    # tokens. Harmless for backends that ignore it.
    if is_stream:
        body["stream_options"] = {"include_usage": True}
    if oai_tools:
        body["tools"] = oai_tools
        tool_choice = _convert_tool_choice(anthro_body.get("tool_choice"))
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    # Anthropic ``stop_sequences`` maps to OpenAI ``stop``.
    stop = anthro_body.get("stop_sequences")
    if stop:
        body["stop"] = stop
    # Anthropic ``thinking`` (extended-thinking config) -> OpenAI reasoning_effort.
    # We do not know whether the backend is Claude-native here; the conservative,
    # widely-accepted mapping for OpenAI-compatible backends is reasoning_effort.
    effort = _convert_thinking(anthro_body.get("thinking"))
    if effort is not None and "reasoning_effort" not in body:
        body["reasoning_effort"] = effort

    # ``metadata.user_id`` is Anthropic's end-user identifier for abuse-tracking
    # / caching attribution. OpenAI's equivalent is the top-level ``user`` field.
    # We do NOT pass the rest of ``metadata`` through — leaking an Anthropic-shaped
    # field to an OpenAI backend can confuse strict validators (see proxy_config
    # for the broader "don't leak shapes across protocols" stance). Caller can
    # still set a top-level ``user`` directly; we only fill it from metadata when
    # absent so an explicit value wins.
    metadata = anthro_body.get("metadata")
    if isinstance(metadata, dict):
        user_id = metadata.get("user_id")
        if isinstance(user_id, str) and user_id and "user" not in body:
            body["user"] = user_id

    return body


def _convert_thinking(thinking: Any) -> str | None:
    """Map an Anthropic ``thinking`` block to an OpenAI ``reasoning_effort`` level.

    Anthropic shape: ``{"type": "enabled", "budget_tokens": N}``. OpenAI-style
    backends accept ``low|medium|high``; bucket by budget so a larger thinking
    budget asks for more reasoning. Returns ``None`` when thinking is absent or
    disabled.
    """
    if not isinstance(thinking, dict):
        return None
    if thinking.get("type") not in ("enabled", "adaptive"):
        return None
    budget = thinking.get("budget_tokens")
    if not isinstance(budget, int):
        return "medium"
    if budget <= 4096:
        return "low"
    if budget <= 16384:
        return "medium"
    return "high"


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Map an Anthropic ``tool_choice`` to its OpenAI equivalent.

    Anthropic shapes: ``{"type": "auto"|"any"|"none"}`` or
    ``{"type": "tool", "name": "<tool>"}``. OpenAI wants ``"auto"``/``"none"``/
    ``"required"`` strings, or ``{"type": "function", "function": {"name": ...}}``.
    Returns ``None`` when there is nothing to forward.
    """
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "none":
        return "none"
    if choice_type == "any":
        return "required"
    if choice_type == "tool" and tool_choice.get("name"):
        # Match the truncation applied to the tool definitions so the forced
        # choice still references a tool the backend knows.
        return {"type": "function", "function": {"name": truncate_tool_name(tool_choice["name"])}}
    return None


def _merge_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive system messages into one.

    If ANY system message carries list-shape content (e.g. a system block array
    with ``cache_control`` preserved by _convert_system), the merged result keeps
    list shape — concatenating typed parts in order. Otherwise we fall back to
    the simple newline-joined string for maximum backend compatibility.
    """
    if not messages:
        return []
    has_list = any(isinstance(m.get("content"), list) for m in messages)
    if has_list:
        merged_parts: list[Any] = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):
                merged_parts.extend(c)
            elif c:
                merged_parts.append({"type": "text", "text": str(c)})
        return [{"role": "system", "content": merged_parts}] if merged_parts else []
    parts = [str(m.get("content", "")) for m in messages if m.get("content")]
    return [{"role": "system", "content": "\n".join(parts)}] if parts else []


def _convert_system(system: Any) -> dict[str, Any] | None:
    """Convert Anthropic ``system`` (str or array of text blocks) to OpenAI.

    Default: flatten to a single string for maximum backend compatibility (most
    OpenAI-compatible backends accept either string or list, but string is the
    LCD). If ANY block carries ``cache_control`` we keep the per-part shape and
    attach the cache_control field — Anthropic-aware backends read it for prompt
    caching; OpenAI-only backends ignore the unknown field. Without this an
    explicit cache_control is silently lost (a real cost regression).
    """
    if not system:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    if isinstance(system, list):
        # Collect typed parts with cache_control preserved.
        typed_parts: list[dict[str, Any]] = []
        has_cache_control = False
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                part: dict[str, Any] = {"type": "text", "text": block.get("text", "")}
                cc = block.get("cache_control")
                if cc is not None:
                    part["cache_control"] = cc
                    has_cache_control = True
                typed_parts.append(part)
            elif isinstance(block, str):
                typed_parts.append({"type": "text", "text": block})
        if not typed_parts:
            return None
        if has_cache_control:
            return {"role": "system", "content": typed_parts}
        # No cache_control anywhere → flatten back to a string (LCD shape).
        return {"role": "system", "content": "\n".join(p["text"] for p in typed_parts)}
    return None


def _convert_image_block(block: dict) -> dict[str, Any] | None:
    """Convert an Anthropic image block to an OpenAI ``image_url`` content part.

    Anthropic shapes:
      ``{"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}``
      ``{"type":"image","source":{"type":"url","url":"https://..."}}``
    OpenAI wants ``{"type":"image_url","image_url":{"url": <data-uri or url>}}``.
    Returns ``None`` when the source is missing/unrecognized.
    """
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "url" and isinstance(source.get("url"), str):
        url = source["url"]
    elif stype == "base64" and isinstance(source.get("data"), str):
        media_type = source.get("media_type") or "image/png"
        url = f"data:{media_type};base64,{source['data']}"
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _convert_message(msg: dict) -> list[dict]:
    """Convert one Anthropic message to one or more OpenAI messages.

    Historical ``thinking`` blocks are passed through as ``reasoning_content``
    text so reasoning-capable backends can preserve them across turns.
    """
    role = msg.get("role", "user")
    content = msg.get("content", "")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content)}]

    # Complex content with blocks
    text_parts = []
    tool_uses = []
    tool_results = []
    thinking_parts = []
    # Structured thinking blocks preserve Anthropic's per-block ``signature``
    # (and ``redacted_thinking.data``) so a multi-turn round-trip to a Claude
    # backend can echo them back unchanged. Carried on the OpenAI message as a
    # ``thinking_blocks`` field; OpenAI-only backends ignore unknown fields, but
    # any backend that round-trips messages preserves the signature for the next
    # request.
    thinking_blocks: list[dict[str, Any]] = []
    image_parts: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            thinking_parts.append(block.get("thinking", ""))
            tb: dict[str, Any] = {"type": "thinking", "thinking": block.get("thinking", "")}
            if block.get("signature"):
                tb["signature"] = block["signature"]
            thinking_blocks.append(tb)
        elif btype == "redacted_thinking":
            # Anthropic emits these when the model's thinking was redacted; the
            # opaque ``data`` blob must round-trip verbatim, but there is no text
            # for ``reasoning_content``. Only the structured carry survives.
            thinking_blocks.append({
                "type": "redacted_thinking",
                "data": block.get("data", ""),
            })
        elif btype == "tool_use":
            tool_uses.append(block)
        elif btype == "tool_result":
            tool_results.append(block)
        elif btype == "image":
            image_part = _convert_image_block(block)
            if image_part is not None:
                image_parts.append(image_part)
            else:
                log.warning("Dropping image block with unrecognized source (role=%s)", role)
        else:
            # Unknown block type: skip rather than dumping raw JSON into the
            # prompt, which would pollute the model's context. Warn for audit.
            log.warning("Dropping unsupported content block type %r (role=%s)", btype, role)

    results = []

    if role == "assistant" and tool_uses:
        oai_msg: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
            "tool_calls": [],
        }
        if thinking_parts:
            thinking_text = "\n".join(thinking_parts)
            # Carry thinking under BOTH reasoning keys: OpenAI-compatible backends
            # disagree on which one they read back for cross-turn passback (some
            # honor reasoning_content, others reasoning), and we don't know which
            # this backend checks. Whichever it reads is populated; the other is
            # a harmless unknown field.
            oai_msg["reasoning_content"] = thinking_text
            oai_msg["reasoning"] = thinking_text
        if thinking_blocks:
            oai_msg["thinking_blocks"] = thinking_blocks
        for tu in tool_uses:
            oai_msg["tool_calls"].append({
                "id": tu.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                "type": "function",
                "function": {
                    # Truncate to match the (truncated) name in the tools[] array
                    # — history tool_calls must reference the same name the backend
                    # was told about, or strict backends reject the request.
                    "name": truncate_tool_name(tu.get("name", "")),
                    "arguments": json.dumps(tu.get("input", {})),
                },
            })
        results.append(oai_msg)

    elif role == "user" and tool_results:
        if text_parts or image_parts:
            if image_parts:
                content_value: Any = []
                if text_parts:
                    content_value.append({"type": "text", "text": "\n".join(text_parts)})
                content_value.extend(image_parts)
            else:
                content_value = "\n".join(text_parts)
            results.append({"role": "user", "content": content_value})
        for tr in tool_results:
            tr_content = tr.get("content", "")
            # If the tool returned image blocks (screenshot tools, computer-use,
            # etc.), build OpenAI multi-part content [text..., image_url...].
            # Anthropic's tool_result spec allows mixed text + image inside one
            # tool_result; OpenAI ``role:tool`` also accepts a list of typed
            # parts. Without this branch the image would be json.dumps'd into the
            # tool text (base64 blob → corrupted image + context blowup).
            tool_images: list[dict[str, Any]] = []
            if isinstance(tr_content, list):
                text_buf = []
                for c in tr_content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text_buf.append(c.get("text", ""))
                    elif isinstance(c, dict) and c.get("type") == "image":
                        img = _convert_image_block(c)
                        if img is not None:
                            tool_images.append(img)
                        else:
                            log.warning("Dropping image block with unrecognized source in tool_result")
                    elif isinstance(c, str):
                        text_buf.append(c)
                    else:
                        text_buf.append(json.dumps(c))
                tr_content = "\n".join(text_buf)
            elif not isinstance(tr_content, str):
                tr_content = json.dumps(tr_content)
            # Anthropic marks a failed tool result with ``is_error: true``. The
            # OpenAI ``tool`` message has no such field, so surface the failure
            # in-band; otherwise the model can't tell a failure from a success.
            if tr.get("is_error"):
                tr_content = f"[tool_error] {tr_content}"
            if tool_images:
                # Multi-part: text first (if any), then images.
                content_value: Any = []
                if tr_content:
                    content_value.append({"type": "text", "text": tr_content})
                content_value.extend(tool_images)
            else:
                content_value = tr_content
            results.append({
                "role": "tool",
                "tool_call_id": tr.get("tool_use_id", "unknown"),
                "content": content_value,
            })
    else:
        text = "\n".join(text_parts) if text_parts else ""
        if image_parts:
            # OpenAI multimodal: content must be a list of typed parts. Lead with
            # the text part (if any), then the image parts.
            content_value: Any = []
            if text:
                content_value.append({"type": "text", "text": text})
            content_value.extend(image_parts)
        else:
            content_value = text
        oai_msg = {"role": role, "content": content_value}
        if role == "assistant" and thinking_parts:
            # Carry thinking under both reasoning keys — see the tool_use branch
            # above for why (backends disagree on which key they honor).
            thinking_text = "\n".join(thinking_parts)
            oai_msg["reasoning_content"] = thinking_text
            oai_msg["reasoning"] = thinking_text
        if role == "assistant" and thinking_blocks:
            oai_msg["thinking_blocks"] = thinking_blocks
        results.append(oai_msg)

    return results


def _convert_tools(anthro_tools: list | None) -> list | None:
    if not anthro_tools:
        return None
    oai_tools = []
    for t in anthro_tools:
        oai_tools.append({
            "type": "function",
            "function": {
                # Truncate to OpenAI's 64-char tool-name limit; the response
                # converter maps the truncated name back to the original.
                "name": truncate_tool_name(t.get("name", "")),
                "description": t.get("description", ""),
                "parameters": _sanitize_openai_parameters(t.get("input_schema")),
            },
        })
    return oai_tools


def _sanitize_openai_parameters(schema: Any) -> dict[str, Any]:
    """Normalize Anthropic tool schemas to an OpenAI-compatible subset.

    Claude Code advertises full JSON Schema 2020-12 tool schemas, but many
    OpenAI-compatible backends only accept the function-calling subset. Keep the
    semantic core and drop validator-specific keywords that commonly trigger 400s.
    """
    sanitized = _sanitize_schema_node(schema)
    if not isinstance(sanitized, dict):
        return dict(_DEFAULT_PARAMETERS)

    properties = sanitized.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        sanitized["properties"] = properties

    sanitized["type"] = "object"
    if not properties:
        sanitized.pop("required", None)
    return sanitized


def _sanitize_schema_node(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}

    union_node = _select_union_branch(node)
    if union_node is not node:
        merged = _sanitize_schema_node(union_node)
        if isinstance(node.get("description"), str) and "description" not in merged:
            merged["description"] = node["description"]
        return merged

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _ALLOWED_SCHEMA_KEYS:
            continue
        if key == "type":
            normalized_type = _normalize_schema_type(value)
            if normalized_type:
                out[key] = normalized_type
        elif key == "properties":
            if isinstance(value, dict):
                props = {
                    str(name): _sanitize_schema_node(prop_schema)
                    for name, prop_schema in value.items()
                    if isinstance(name, str)
                }
                out[key] = props
        elif key == "required":
            if isinstance(value, list):
                required = [item for item in value if isinstance(item, str)]
                if required:
                    out[key] = required
        elif key == "items":
            if isinstance(value, dict):
                out[key] = _sanitize_schema_node(value)
            elif isinstance(value, list) and value:
                out[key] = _sanitize_schema_node(value[0])
        elif key == "enum":
            if isinstance(value, list):
                out[key] = value
        elif key == "description":
            if isinstance(value, str):
                out[key] = value
        elif key == "format":
            if isinstance(value, str):
                out[key] = value
        elif key == "default":
            # Advisory default; pass through any JSON value as-is.
            out[key] = value

    properties = out.get("properties")
    if isinstance(properties, dict):
        required = out.get("required")
        if isinstance(required, list):
            required = [name for name in required if name in properties]
            if required:
                out["required"] = required
            else:
                out.pop("required", None)
        if "type" not in out:
            out["type"] = "object"

    return out


def _merge_all_of(branches: list[Any]) -> dict[str, Any]:
    """Combine ``allOf`` branches into one node (intersection semantics).

    ``allOf`` means the value must satisfy *every* branch, so picking a single
    branch would silently drop constraints. We shallow-merge object branches:
    union the ``properties`` and ``required`` lists, keep the first concrete
    ``type``, and keep the first occurrence of the other allowed scalar keywords
    (``enum`` / ``items`` / ``format`` / ``default`` / ``description``) so a branch
    that only carries e.g. an enum is not lost. Nested schemas are sanitized later
    by the caller.
    """
    merged: dict[str, Any] = {}
    properties: dict[str, Any] = {}
    required: list[Any] = []
    # Non-property allowed keywords: keep the first branch that provides each.
    _CARRY_KEYS = ("enum", "items", "format", "default", "description")
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        if "type" not in merged:
            btype = _normalize_schema_type(branch.get("type"))
            if btype:
                merged["type"] = btype
        if isinstance(branch.get("properties"), dict):
            properties.update(branch["properties"])
        if isinstance(branch.get("required"), list):
            for name in branch["required"]:
                if name not in required:
                    required.append(name)
        for key in _CARRY_KEYS:
            if key not in merged and key in branch:
                merged[key] = branch[key]
    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged


def _select_union_branch(node: dict[str, Any]) -> Any:
    for key in _SCHEMA_UNION_KEYS:
        branches = node.get(key)
        if not isinstance(branches, list):
            continue
        if key == "allOf":
            # Intersection: merge all branches rather than picking one.
            return _merge_all_of(branches)
        # anyOf / oneOf: pick the first non-null branch (schema flattening).
        for branch in branches:
            if isinstance(branch, dict) and _normalize_schema_type(branch.get("type")) != "null":
                return branch
        return {}
    return node


def _normalize_schema_type(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value != "null" else None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item != "null":
                return item
    return None


# ---------------------------------------------------------------------------
# Response: OpenAI -> Anthropic
# ---------------------------------------------------------------------------


def _positive_int(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _from_details(usage: dict, field_names: tuple[str, ...]) -> int:
    """Pull a positive token count from ``usage.prompt_tokens_details.<field>``."""
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    if not isinstance(details, dict):
        return 0
    for fn in field_names:
        v = _positive_int(details.get(fn))
        if v > 0:
            return v
    return 0


def _anthropic_usage_from_openai(usage: dict | None) -> dict[str, int]:
    """Translate an OpenAI usage dict to Anthropic shape, including cache tokens.

    Mirrors litellm's _translate_openai_usage_to_anthropic_usage_delta:
    - ``cache_read_input_tokens`` from explicit field OR
      ``prompt_tokens_details.cached_tokens``.
    - ``cache_creation_input_tokens`` from explicit field OR
      ``prompt_tokens_details.cache_creation_tokens`` /``cache_write_tokens``.
    - ``input_tokens = max(prompt_tokens - cache_read - cache_creation, 0)`` —
      Anthropic semantics report only the non-cached input under input_tokens, so
      we must subtract cached portions or the total over-counts.
    """
    usage = usage or {}
    cache_read = _positive_int(usage.get("cache_read_input_tokens")) \
        or _from_details(usage, ("cached_tokens",))
    cache_creation = _positive_int(usage.get("cache_creation_input_tokens")) \
        or _from_details(usage, ("cache_creation_tokens", "cache_write_tokens"))
    prompt = _positive_int(usage.get("prompt_tokens"))
    input_tokens = max(prompt - cache_read - cache_creation, 0)
    out: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": _positive_int(usage.get("completion_tokens")),
    }
    if cache_read > 0:
        out["cache_read_input_tokens"] = cache_read
    if cache_creation > 0:
        out["cache_creation_input_tokens"] = cache_creation
    return out

def convert_response_o2a(
    oai_resp: dict[str, Any], model: str = "", tool_name_map: dict[str, str] | None = None
) -> dict[str, Any]:
    """Convert an OpenAI chat completion response to Anthropic Messages format.

    ``tool_name_map`` maps truncated tool names (sent to the backend) back to the
    original Anthropic names so the client can match the call to the tool it
    registered.
    """
    tool_name_map = tool_name_map or {}
    choice = (oai_resp.get("choices") or [{}])[0]
    message = choice.get("message", {})
    usage = oai_resp.get("usage", {})

    content = []

    # Structured ``thinking_blocks`` (litellm/Claude shape) preserve per-block
    # ``signature`` and ``redacted_thinking.data``. Prefer them over flat
    # ``reasoning_content`` text when the backend provides them, so a multi-turn
    # round-trip back to a Claude backend keeps the signature intact (Anthropic
    # rejects/ignores a thinking block whose signature was stripped).
    structured_blocks = message.get("thinking_blocks")
    if isinstance(structured_blocks, list) and structured_blocks:
        for tb in structured_blocks:
            if not isinstance(tb, dict):
                continue
            tb_type = tb.get("type")
            if tb_type == "thinking":
                blk: dict[str, Any] = {"type": "thinking", "thinking": tb.get("thinking", "")}
                if tb.get("signature"):
                    blk["signature"] = tb["signature"]
                content.append(blk)
            elif tb_type == "redacted_thinking":
                content.append({"type": "redacted_thinking", "data": tb.get("data", "")})
    else:
        # Fallback: plain reasoning text (no signature available from backend).
        reasoning_text = message.get("reasoning_content") or message.get("reasoning")
        if reasoning_text:
            content.append({"type": "thinking", "thinking": str(reasoning_text)})

    # Text
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # Tool calls. Use ``or []`` (not a .get default) because some backends
    # send ``"tool_calls": null`` explicitly — a present-but-null key bypasses
    # the default and would yield None.
    tool_calls = message.get("tool_calls") or []

    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            input_data = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            input_data = {"raw": fn.get("arguments", "")}
        name = fn.get("name", "")
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:20]}"),
            "name": tool_name_map.get(name, name),
            "input": input_data,
        })

    finish = choice.get("finish_reason", "stop")
    stop_reason = _map_finish_reason(finish, bool(tool_calls))

    if not content:
        content.append({"type": "text", "text": ""})

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": oai_resp.get("model", model),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _anthropic_usage_from_openai(usage),
    }


def _map_finish_reason(finish_reason: str | None, has_tool_calls: bool = False) -> str:
    # ``function_call`` is OpenAI's legacy single-call finish reason (superseded by
    # ``tool_calls``) — it is still a tool invocation, so map it to tool_use.
    if finish_reason in ("tool_calls", "function_call") or has_tool_calls:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


# ---------------------------------------------------------------------------
# Streaming: OpenAI SSE chunks -> Anthropic SSE events
# ---------------------------------------------------------------------------

class StreamingA2OConverter:
    """Converts streaming OpenAI chunks to Anthropic SSE events.

    Usage:
        converter = StreamingA2OConverter(model="my-model")
        yield converter.start()
        for chunk in openai_stream:
            for event in converter.feed(chunk):
                yield event
        for event in converter.finish():
            yield event
    """

    def __init__(self, model: str = "", tool_name_map: dict[str, str] | None = None):
        self.model = model
        self.tool_name_map = tool_name_map or {}
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        # Anthropic-shape usage state; computed from each backend usage chunk via
        # _anthropic_usage_from_openai so cache_read/creation tokens are pulled
        # from explicit fields OR prompt_tokens_details.cached_tokens, and the
        # cached portions are subtracted from input_tokens (Anthropic semantics).
        self._usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.finish_reason: str | None = None
        self.content: list[dict] = []
        self._text_idx: int | None = None
        self._text_open = False
        self._thinking_idx: int | None = None
        self._thinking_open = False
        self._tool_blocks: dict[int, dict] = {}

    # Back-compat read-only views — earlier callers (and the thorough proxy test
    # suite) treated input_tokens/output_tokens as instance attributes. The
    # underlying state moved to ``_usage`` so cache_read/creation tokens can be
    # carried too, but the two basic counts stay readable here so external code
    # that only needs the totals doesn't break.
    @property
    def input_tokens(self) -> int:
        return self._usage.get("input_tokens", 0)

    @property
    def output_tokens(self) -> int:
        return self._usage.get("output_tokens", 0)

    def start(self) -> bytes:
        """Emit message_start event."""
        return _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model,
                "stop_reason": None,
                "stop_sequence": None,
                # usage is 0 here by design — upstream hasn't sent its final usage
                # chunk yet (matches real Anthropic streaming, which settles tokens
                # at the end). The real counts are emitted in finish()'s
                # message_delta. Not a bug; do not "fix" this to non-zero.
                "usage": {"input_tokens": self._usage.get("input_tokens", 0), "output_tokens": 0},
            },
        })

    def feed(self, chunk: dict[str, Any]) -> list[bytes]:
        """Process one OpenAI chunk and return zero or more Anthropic SSE events."""
        events: list[bytes] = []
        if not isinstance(chunk, dict):
            return events

        self.model = chunk.get("model", self.model)
        usage = chunk.get("usage")
        if usage:
            # Translate this usage chunk to Anthropic shape (handles cache_read /
            # cache_creation + subtracts cached portions from input_tokens). Keep
            # the latest non-zero counts so we don't overwrite a final usage
            # chunk with a leading 0-count one.
            translated = _anthropic_usage_from_openai(usage)
            for k, v in translated.items():
                if v or k not in self._usage:
                    self._usage[k] = v

        choices = chunk.get("choices", [])
        if not choices:
            return events

        choice = choices[0]
        delta = choice.get("delta", {})

        # Reasoning -> Anthropic thinking block.
        reasoning_text = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning_text:
            idx, start_evts = self._ensure_thinking()
            events.extend(start_evts)
            self.content[idx]["thinking"] += str(reasoning_text)
            events.append(_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "thinking_delta", "thinking": str(reasoning_text)},
            }))

        # Content text
        content_text = delta.get("content")
        if content_text:
            events.extend(self._close_thinking())
            idx, start_evts = self._ensure_text()
            events.extend(start_evts)
            self.content[idx]["text"] += content_text
            events.append(_sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": content_text},
            }))

        # Tool calls. ``or []`` guards against a present-but-null tool_calls
        # field in the delta (some backends send ``"tool_calls": null``).
        for tc_delta in (delta.get("tool_calls") or []):
            tc_idx = tc_delta.get("index", 0)
            events.extend(self._close_thinking())
            events.extend(self._close_text())
            state, start_evts = self._ensure_tool(tc_idx, tc_delta)
            events.extend(start_evts)
            args_delta = (tc_delta.get("function") or {}).get("arguments", "")
            if args_delta:
                state["args"] += args_delta
                events.append(_sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": state["block_idx"],
                    "delta": {"type": "input_json_delta", "partial_json": args_delta},
                }))

        # Finish reason
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        return events

    def finish(self) -> list[bytes]:
        """Emit final events (close blocks + message_delta + message_stop)."""
        events: list[bytes] = []
        events.extend(self._close_thinking())
        events.extend(self._close_text())
        for state in sorted(self._tool_blocks.values(), key=lambda s: s["block_idx"]):
            if state.get("open"):
                state["open"] = False
                events.append(_sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": state["block_idx"],
                }))

        stop_reason = _map_finish_reason(self.finish_reason, bool(self._tool_blocks))
        events.append(_sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            # Report token counts collected from the upstream final usage chunk
            # (stream_options.include_usage). Anthropic clients reading usage from
            # message_delta otherwise see input_tokens=0. Includes
            # cache_read_input_tokens / cache_creation_input_tokens when the
            # backend reported them.
            "usage": dict(self._usage),
        }))
        events.append(_sse_event("message_stop", {"type": "message_stop"}))
        return events

    def build_final_response(self) -> dict[str, Any]:
        """Build the complete Anthropic response from accumulated state."""
        finalized = []
        tool_by_idx = {s["block_idx"]: s for s in self._tool_blocks.values()}
        for i, block in enumerate(self.content):
            if block["type"] == "tool_use":
                state = tool_by_idx[i]
                raw = state.get("args", "")
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                finalized.append({
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": parsed,
                })
            else:
                finalized.append(block)

        if not finalized:
            finalized.append({"type": "text", "text": ""})

        return {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "content": finalized,
            "model": self.model,
            "stop_reason": _map_finish_reason(self.finish_reason, bool(self._tool_blocks)),
            "stop_sequence": None,
            "usage": dict(self._usage),
        }

    # --- internal block management ---

    def _new_block(self, block: dict) -> int:
        self.content.append(block)
        return len(self.content) - 1

    def _ensure_text(self) -> tuple[int, list[bytes]]:
        events: list[bytes] = []
        if self._text_idx is None or not self._text_open:
            self._text_idx = self._new_block({"type": "text", "text": ""})
            self._text_open = True
            events.append(_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": self._text_idx,
                "content_block": {"type": "text", "text": ""},
            }))
        return self._text_idx, events

    def _close_text(self) -> list[bytes]:
        if not self._text_open or self._text_idx is None:
            return []
        self._text_open = False
        return [_sse_event("content_block_stop", {
            "type": "content_block_stop", "index": self._text_idx,
        })]

    def _ensure_thinking(self) -> tuple[int, list[bytes]]:
        events: list[bytes] = []
        if self._thinking_idx is None or not self._thinking_open:
            self._thinking_idx = self._new_block({"type": "thinking", "thinking": ""})
            self._thinking_open = True
            events.append(_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": self._thinking_idx,
                "content_block": {"type": "thinking", "thinking": ""},
            }))
        return self._thinking_idx, events

    def _close_thinking(self) -> list[bytes]:
        if not self._thinking_open or self._thinking_idx is None:
            return []
        self._thinking_open = False
        return [_sse_event("content_block_stop", {
            "type": "content_block_stop", "index": self._thinking_idx,
        })]

    def _ensure_tool(self, oai_idx: int, delta: dict) -> tuple[dict, list[bytes]]:
        events: list[bytes] = []
        fn = delta.get("function", {})
        if oai_idx not in self._tool_blocks:
            raw_name = fn.get("name", "")
            block = {
                "type": "tool_use",
                "id": delta.get("id", f"toolu_{uuid.uuid4().hex[:20]}"),
                "name": self.tool_name_map.get(raw_name, raw_name),
                "input": {},
            }
            block_idx = self._new_block(block)
            state = {"block_idx": block_idx, "args": "", "open": True}
            self._tool_blocks[oai_idx] = state
            events.append(_sse_event("content_block_start", {
                "type": "content_block_start",
                "index": block_idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": {},
                },
            }))
        else:
            state = self._tool_blocks[oai_idx]
            block = self.content[state["block_idx"]]
            if fn.get("name") and not block.get("name"):
                block["name"] = self.tool_name_map.get(fn["name"], fn["name"])
        return state, events


def _sse_event(event: str, data: dict) -> bytes:
    """Format one Anthropic SSE event as bytes."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
