"""SecretsPort: leitura de credenciais. O valor NUNCA deve ser logado."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsPort(Protocol):
    name: str

    async def get(self, key: str) -> str: ...
