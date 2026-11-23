"""Hierarquia de erros de domínio do DengCode.

Adapters convertem exceções de SDKs nestes erros: nenhuma exceção crua de
biblioteca externa atravessa uma porta.
"""

from __future__ import annotations


class DengCodeError(Exception):
    """Raiz de todos os erros do DengCode."""


class ConfigError(DengCodeError):
    """Configuração inválida (stack, PROJETO.md, variáveis de ambiente)."""


class AdapterError(DengCodeError):
    """Falha genérica dentro de um adapter."""


class AuthError(AdapterError):
    """Credencial ausente, inválida ou expirada (401)."""


class NotFoundError(AdapterError):
    """Recurso inexistente (tabela, arquivo, pipeline, modelo...)."""


class TransientError(AdapterError):
    """Erro temporário: vale tentar de novo (5xx, timeout, conexão)."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class RateLimitError(TransientError):
    """Limite de requisições atingido (429)."""


class PermissionDeniedError(DengCodeError):
    """Ação bloqueada pela política ou pelo provedor (403)."""


class CostLimitExceeded(DengCodeError):
    """A estimativa de custo passou do teto configurado."""


class UnsupportedOperation(AdapterError):
    """O adapter não oferece esta operação (ver capabilities)."""


class BudgetExceeded(DengCodeError):
    """O orçamento da sessão (passos, tokens ou US$) acabou."""


class ScopeError(DengCodeError):
    """Operação de memória sem escopo de workspace válido."""
