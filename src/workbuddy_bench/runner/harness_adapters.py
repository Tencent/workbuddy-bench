"""Centralized harness runtime adapter mapping.

Maps ``harness.name`` (from configs/harnesses/<slug>.yaml) to normalized
runtime metadata that ``scripts/run.sh`` needs for backend wiring, proxy
binding, and log display.

This module is the *single source of truth* for harness-specific runtime
behavior. Adding a new harness means adding one entry to HARNESS_ADAPTERS —
no more editing scattered shell branches.

Usage from shell (called by run.sh)::

    python3 -m workbuddy_bench.runner.harness_adapters <harness-name>

Prints key=value pairs suitable for ``eval "$(...)"``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HarnessRuntimeAdapter:
    """Normalized runtime metadata for a single harness type."""

    # Identity
    harness_name: str
    canonical_display_name: str

    # Protocol the harness speaks
    harness_protocol: str  # "openai" or "anthropic"

    # Backend env-var binding: how the harness receives the model URL/key.
    # For proxy mode these point to the proxy URL; for direct mode the values
    # come from the model's backend_url_env / backend_key_env.
    backend_base_env: str  # env var name the harness reads for base URL
    backend_key_env: str   # env var name the harness reads for API key

    # Proxy URL env: set when the harness should route through the proxy.
    # Empty string means the harness doesn't use a separate proxy env.
    proxy_url_env: str

    # Whether the harness uses ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL.
    uses_anthropic_env: bool

    def emit_shell_vars(self) -> str:
        """Return shell-eval-able key=value assignments."""
        lines = [
            f"HARNESS_NAME={self.harness_name!r}",
            f"HARNESS_DISPLAY_NAME={self.canonical_display_name!r}",
            f"HARNESS_PROTOCOL={self.harness_protocol!r}",
            f"HARNESS_BACKEND_BASE_ENV={self.backend_base_env!r}",
            f"HARNESS_BACKEND_KEY_ENV={self.backend_key_env!r}",
            f"HARNESS_PROXY_URL_ENV={self.proxy_url_env!r}",
            f"HARNESS_USES_ANTHROPIC_ENV={'1' if self.uses_anthropic_env else ''}",
        ]
        return "\n".join(lines)


# Adapter definitions

HARNESS_ADAPTERS: dict[str, HarnessRuntimeAdapter] = {
    "claude-code": HarnessRuntimeAdapter(
        harness_name="claude-code",
        canonical_display_name="Claude Code",
        harness_protocol="anthropic",
        backend_base_env="ANTHROPIC_BASE_URL",
        backend_key_env="ANTHROPIC_API_KEY",
        proxy_url_env="",  # sets ANTHROPIC_BASE_URL directly
        uses_anthropic_env=True,
    ),
    "cbc-agent": HarnessRuntimeAdapter(
        harness_name="cbc-agent",
        canonical_display_name="CodeBuddy Code CLI",
        harness_protocol="openai",
        backend_base_env="CBC_BASE_URL",
        backend_key_env="CBC_API_KEY",
        proxy_url_env="CBC_PROXY_URL",
        uses_anthropic_env=False,
    ),
}


def resolve_adapter(harness_name: str) -> HarnessRuntimeAdapter:
    """Look up the adapter for a given harness.name.

    Raises ValueError with a clear message if the name is not registered.
    """
    adapter = HARNESS_ADAPTERS.get(harness_name)
    if adapter is None:
        valid = ", ".join(sorted(HARNESS_ADAPTERS.keys()))
        raise ValueError(
            f"Unknown harness name: {harness_name!r}. "
            f"Valid harness names: [{valid}]. "
            f"To add a new harness, register an entry in "
            f"src/workbuddy_bench/runner/harness_adapters.py::HARNESS_ADAPTERS."
        )
    return adapter


# CLI interface — called by run.sh via eval "$(python3 -m ...)"

def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: python3 -m workbuddy_bench.runner.harness_adapters <harness-name>",
            file=sys.stderr,
        )
        return 1

    harness_name = sys.argv[1]
    try:
        adapter = resolve_adapter(harness_name)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(adapter.emit_shell_vars())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
