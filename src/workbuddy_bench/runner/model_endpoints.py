"""Helpers for normalizing model client endpoint URLs.

The runner stores proxy roots and backend bases in a protocol-neutral form
(``http://host:port`` or ``https://backend``). Callers then adapt that root to
the wire shape their client expects.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

CONTAINER_HOST_GATEWAY_NAME = "host.docker.internal"
HOST_LOOPBACK_NAME = "127.0.0.1"


def openai_api_base_url(url: str) -> str:
    """Return an OpenAI-compatible ``/v1`` API base URL.

    Accepts either a root URL (``http://host:3456``), an API base
    (``.../v1``), or a chat-completions endpoint
    (``.../v1/chat/completions``). Empty input stays empty so callers can keep
    their existing "unset means disabled" checks.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def openai_chat_completions_url(url: str) -> str:
    """Return the OpenAI Chat Completions endpoint for ``url``.

    This is the endpoint shape cbc expects in ``models.json``. It shares the
    same normalization as host-side and verifier-side judge clients, then
    appends ``/chat/completions``.
    """
    base = openai_api_base_url(url)
    return f"{base}/chat/completions" if base else ""


def host_reachable_url(url: str) -> str:
    """Return a URL that a host process can use for a local Docker gateway URL."""
    raw_url = str(url or "").strip()
    parsed_url = urlsplit(raw_url)
    if parsed_url.hostname != CONTAINER_HOST_GATEWAY_NAME:
        return raw_url
    port = f":{parsed_url.port}" if parsed_url.port else ""
    return urlunsplit(
        parsed_url._replace(netloc=f"{HOST_LOOPBACK_NAME}{port}")
    )
