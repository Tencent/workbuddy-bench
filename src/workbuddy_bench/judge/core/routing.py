"""Verifier-side LLM route contract shared by judge profiles and runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


VERIFIER_LLM_ENV_PREFIX = "WORKBUDDY_VERIFIER_LLM_"


@dataclass(frozen=True)
class VerifierLLMRoute:
    """OpenAI-compatible route injected into verifier-side judges."""

    base_url: str
    api_key: str
    model: str
    max_output_tokens: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def to_env(self, prefix: str = VERIFIER_LLM_ENV_PREFIX) -> dict[str, str]:
        env = {
            f"{prefix}BASE_URL": self.base_url,
            f"{prefix}API_KEY": self.api_key,
            f"{prefix}MODEL": self.model,
        }
        if self.max_output_tokens is not None:
            env[f"{prefix}MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
        return env


def verifier_llm_route_from_mapping(
    values: Mapping[str, object],
    *,
    prefix: str = VERIFIER_LLM_ENV_PREFIX,
) -> VerifierLLMRoute:
    """Parse the verifier LLM route from an env-like mapping."""

    def value(name: str) -> str:
        raw = values.get(f"{prefix}{name}")
        return str(raw or "").strip()

    raw_max = value("MAX_OUTPUT_TOKENS")
    try:
        max_output_tokens = max(1, int(raw_max)) if raw_max else None
    except ValueError:
        max_output_tokens = None

    return VerifierLLMRoute(
        base_url=value("BASE_URL"),
        api_key=value("API_KEY"),
        model=value("MODEL"),
        max_output_tokens=max_output_tokens,
    )
