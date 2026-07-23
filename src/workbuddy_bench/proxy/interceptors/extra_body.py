"""Extra body injection interceptor."""

from __future__ import annotations

import logging

from . import RequestContext, ResponseContext, StreamChunk

log = logging.getLogger("proxy.interceptor.extra_body")


class ExtraBodyInterceptor:
    """Marker interceptor for the ``inject_extra_body`` route hook.

    All injection happens in ``Pipeline._prepare_upstream`` (the single source of
    truth for the upstream body), so every hook here is a no-op. The class stays
    registrable so route configs that list ``inject_extra_body`` remain valid.
    """

    name = "inject_extra_body"

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        return ctx

    async def on_response(self, ctx: RequestContext, resp: ResponseContext) -> ResponseContext:
        return resp

    async def on_stream_chunk(self, ctx: RequestContext, chunk: StreamChunk) -> StreamChunk:
        return chunk

    async def on_stream_end(self, ctx: RequestContext, resp: ResponseContext) -> None:
        pass
