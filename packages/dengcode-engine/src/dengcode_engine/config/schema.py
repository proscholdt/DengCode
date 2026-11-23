"""Schema pydantic de uma stack (docs/especificacoes/stacks.md)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLACEHOLDER_MARK = "<"


class LLMModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    main: str
    fast: str | None = None


class LLMConfig(BaseModel):
    """``provider`` + ``model`` (atalho) ou ``models``; chaves extras vão ao adapter."""

    model_config = ConfigDict(extra="allow")
    provider: str
    model: str | None = None
    models: LLMModels | None = None
    fallback: LLMConfig | None = None

    @model_validator(mode="after")
    def _shortcut(self) -> LLMConfig:
        if self.models is None and self.model is not None:
            self.models = LLMModels(main=self.model, fast=self.model)
        return self

    def adapter_config(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str
    model: str | None = None


class AdapterRef(BaseModel):
    """``{adapter: nome, variant: ..., <chaves livres do adapter>}``."""

    model_config = ConfigDict(extra="allow")
    adapter: str
    variant: str | None = None

    def adapter_config(self) -> dict[str, Any]:
        data = dict(self.model_extra or {})
        if self.variant is not None:
            data["variant"] = self.variant
        return data


class CostLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usd: float | None = None
    bytes: int | None = None


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials_profile: str | None = None
    permission_mode: Literal["ask", "auto", "read_only"] = "ask"
    read_only: bool = False
    allow_prod_writes: bool = False
    cost_limit: CostLimit | None = None
    overrides: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: dict[str, dict[str, Any]]()
    )


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protected_tables: list[str] = Field(default_factory=lambda: list[str]())
    shell_allowlist: list[str] | None = None


class StackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    extends: str | None = None
    description: str | None = None
    llm: LLMConfig | None = None
    embedding: EmbeddingConfig | None = None
    storage: AdapterRef | None = None
    table_format: Literal["delta", "iceberg", "hudi", "parquet"] | None = None
    compute: list[AdapterRef] = Field(default_factory=lambda: list[AdapterRef]())
    orchestrator: AdapterRef | None = None
    catalog: AdapterRef | None = None
    transform: AdapterRef | None = None
    streaming: AdapterRef | None = None
    stream_compute: list[AdapterRef] = Field(default_factory=lambda: list[AdapterRef]())
    secrets: AdapterRef | None = None
    memory: AdapterRef = Field(default_factory=lambda: AdapterRef(adapter="sqlite"))
    environments: dict[str, EnvironmentConfig] = Field(
        default_factory=lambda: {"dev": EnvironmentConfig()}
    )
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    def environment(self, env: str) -> EnvironmentConfig:
        if env in self.environments:
            return self.environments[env]
        from dengcode_core.errors import ConfigError

        known = ", ".join(self.environments) or "nenhum"
        raise ConfigError(f"ambiente '{env}' não existe na stack {self.name} (há: {known})")

    def placeholders(self) -> list[str]:
        """Campos ainda com valores de exemplo (``"<...>"``)."""
        found: list[str] = []

        def walk(prefix: str, value: Any) -> None:
            if isinstance(value, str) and value.strip().startswith(PLACEHOLDER_MARK):
                found.append(prefix)
            elif isinstance(value, dict):
                for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
                    walk(f"{prefix}.{k}" if prefix else str(k), v)  # pyright: ignore[reportUnknownArgumentType]
            elif isinstance(value, list):
                for i, v in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                    walk(f"{prefix}[{i}]", v)

        walk("", self.model_dump(exclude_none=True))
        return found
