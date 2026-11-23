"""DTOs compartilhados pelas portas (pydantic, imutáveis)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from dengcode_core.ports.secrets import SecretsPort


class PortKind(StrEnum):
    """Conceitos que um adapter pode implementar."""

    LLM = "llm"
    EMBEDDING = "embedding"
    STORAGE = "storage"
    QUERY_ENGINE = "query_engine"
    CATALOG = "catalog"
    ORCHESTRATOR = "orchestrator"
    TRANSFORM = "transform"
    STREAM = "stream"
    STREAM_COMPUTE = "stream_compute"
    SECRETS = "secrets"
    MEMORY_STORE = "memory_store"


class Frozen(BaseModel):
    """Base de todos os DTOs: imutável e sem campos extras."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AdapterInfo(Frozen):
    """Metadados que todo adapter declara como ``info`` de classe."""

    name: str
    port: PortKind
    version: str
    knowledge_pack: str | None = None


@dataclass(frozen=True)
class AdapterContext:
    """O que o motor entrega a cada adapter ao construí-lo."""

    workspace: str
    env: str = "dev"
    secrets: SecretsPort | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("dengcode.adapter"))
    cache_dir: Path | None = None
    project_dir: Path | None = None


class StorageCapabilities(Frozen):
    supports_write: bool
    supports_list_recursive: bool
    table_formats: frozenset[str]


class ObjectInfo(Frozen):
    uri: str
    size_bytes: int | None
    modified_at: datetime | None
    is_dir: bool
