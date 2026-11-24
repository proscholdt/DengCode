"""Servidor ACP (Agent Client Protocol) do DengCode.

O core não conhece o ACP: este pacote traduz o EventBus em mensagens JSON-RPC
(ver ``docs/especificacoes/acp.md`` e ADR 0009).
"""

from __future__ import annotations
