"""ModelProfile: flags e preços de cada modelo."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)


class ModelProfile(BaseModel):
    provider: str
    model: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_parallel_tools: bool
    supports_prompt_caching: bool
    supports_vision: bool
    supports_extended_thinking: bool
    price_input_per_mtok_usd: float | None
    price_output_per_mtok_usd: float | None

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.price_input_per_mtok_usd is None or self.price_output_per_mtok_usd is None:
            return None
        return (
            input_tokens * self.price_input_per_mtok_usd
            + output_tokens * self.price_output_per_mtok_usd
        ) / 1_000_000


def conservative_profile(provider: str, model: str) -> ModelProfile:
    """Perfil para modelo desconhecido: 32k de contexto, sem tools paralelas."""
    return ModelProfile(
        provider=provider,
        model=model,
        context_window=32_000,
        max_output_tokens=4_096,
        supports_tools=True,
        supports_parallel_tools=False,
        supports_prompt_caching=False,
        supports_vision=False,
        supports_extended_thinking=False,
        price_input_per_mtok_usd=None,
        price_output_per_mtok_usd=None,
    )


def resolve_profile(
    provider: str,
    model: str,
    table: Mapping[str, Mapping[str, Any]],
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> ModelProfile:
    """Busca o perfil na tabela do adapter (match exato ou por prefixo) + overrides da stack."""
    base: dict[str, Any] | None = None
    if model in table:
        base = dict(table[model])
    else:
        prefixes = sorted((k for k in table if model.startswith(k)), key=len, reverse=True)
        if prefixes:
            base = dict(table[prefixes[0]])
    if base is None:
        log.warning("modelo %s/%s sem perfil conhecido; usando perfil conservador", provider, model)
        profile = conservative_profile(provider, model)
    else:
        profile = ModelProfile(provider=provider, model=model, **base)
    extra = (overrides or {}).get(model)
    return profile.model_copy(update=dict(extra)) if extra else profile
