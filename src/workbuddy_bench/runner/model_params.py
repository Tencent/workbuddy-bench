"""Model request-parameter flattening — shared by every consumer that turns a
model's ``params`` block into an upstream request body.

Kept in its own module (not resolve_manifest) because three layers need it and
none should reach into another's internals for it: resolve_manifest builds the
proxy's injected extra_body, proxy_config builds the judge route, and the scorer's
llm_judge inlines params on direct transport. They all import ``flatten_params``
from here rather than a private helper on a sibling module.
"""

from __future__ import annotations

import copy
from typing import Any

# ``model.params`` is the single declaration point for a model's request
# parameters. Two kinds of keys are not request-body params and are handled
# separately rather than blacklisted ad hoc:
#   BEHAVIOR_PARAMS — cbc behaviour toggles (written to settings.json, not the
#     request body). ``thinking_enabled`` -> settings.json alwaysThinkingEnabled.
#   EXTRA_BODY_KEY  — a *container*; its contents are flattened into the body,
#     the key itself is not.
# Everything else in ``params`` is a request param and is injected. Notably
# ``max_output_tokens`` is a request param: it is renamed to the OpenAI body
# field ``max_tokens`` (see flatten_params). Under local_proxy it is injected
# by the proxy; under direct cbc forwards it natively via models.json.
BEHAVIOR_PARAMS = ("thinking_enabled",)
EXTRA_BODY_KEY = "extra_body"


def flatten_params(params: dict[str, Any]) -> dict[str, Any]:
    """Flatten a model's ``params`` block into the dict injected (shallow-update)
    into the upstream request body.

    Injects every param key except behaviour toggles (BEHAVIOR_PARAMS) and the
    ``extra_body`` container key, then flattens ``extra_body``'s contents on top.
    ``max_output_tokens`` is renamed to the OpenAI body field ``max_tokens``.

    Precedence on collision: ``extra_body`` contents win over flattened top-level
    keys (extra_body is the more specific, provider-targeted source) — so an
    explicit ``extra_body.max_tokens`` overrides the mapped ``max_output_tokens``.
    """
    if not isinstance(params, dict):
        return {}
    _excluded = set(BEHAVIOR_PARAMS) | {EXTRA_BODY_KEY, "max_output_tokens"}
    flat = {k: copy.deepcopy(v) for k, v in params.items() if k not in _excluded}
    # max_output_tokens -> max_tokens (mapped before extra_body so an explicit
    # extra_body.max_tokens wins).
    mot = params.get("max_output_tokens")
    if mot is not None:
        flat["max_tokens"] = mot
    extra = params.get(EXTRA_BODY_KEY)
    if isinstance(extra, dict):
        flat.update(copy.deepcopy(extra))
    return flat
