"""CostGuard: estimativa antes de executar (sqlguard.md, "Proteção de custo").

- engine com dry-run: estima e compara com o teto (``cost_limit``);
- só com explain: guarda o plano para o card de aprovação;
- sem estimativa possível e engine tarifada (``metered``): exige aprovação.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from dengcode_core.errors import DengCodeError
from dengcode_core.ports.models import CostEstimate
from dengcode_core.ports.query_engine import QueryEnginePort
from dengcode_core.sql.guard import SqlAnalysis

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostLimit:
    usd: float | None = None
    bytes: int | None = None


@dataclass(frozen=True)
class CostCheck:
    estimate: CostEstimate | None
    plan: str | None
    reason: str | None  # preenchido quando a política precisa pedir aprovação

    def describe(self) -> str:
        if self.estimate is None:
            return "sem estimativa"
        parts: list[str] = []
        if self.estimate.bytes_scanned is not None:
            parts.append(f"{self.estimate.bytes_scanned / 1e9:.3f} GB")
        if self.estimate.usd is not None:
            parts.append(f"US$ {self.estimate.usd:.4f}")
        if self.estimate.credits is not None:
            parts.append(f"{self.estimate.credits} créditos")
        return " · ".join(parts) or "estimativa vazia"


def over_limit(estimate: CostEstimate, limit: CostLimit) -> str | None:
    if limit.usd is not None and estimate.usd is not None and estimate.usd > limit.usd:
        return f"estimativa US$ {estimate.usd:.4f} acima do teto US$ {limit.usd:.2f}"
    if (
        limit.bytes is not None
        and estimate.bytes_scanned is not None
        and estimate.bytes_scanned > limit.bytes
    ):
        return (
            f"estimativa {estimate.bytes_scanned / 1e9:.2f} GB acima do teto "
            f"{limit.bytes / 1e9:.2f} GB"
        )
    return None


async def check_cost(
    engine: QueryEnginePort,
    sql: str,
    analysis: SqlAnalysis,
    *,
    limit: CostLimit | None = None,
    metered: bool = False,
) -> CostCheck:
    caps = engine.capabilities()
    estimate: CostEstimate | None = None
    plan: str | None = None
    if caps.supports_dry_run:
        try:
            estimate = await engine.dry_run(sql)
        except DengCodeError as exc:
            log.info("dry-run falhou: %s", exc)
    elif caps.supports_explain and analysis.error is None:
        try:
            plan = await engine.explain(sql)
        except DengCodeError as exc:
            log.info("explain falhou: %s", exc)
    reason: str | None = None
    if estimate is not None and limit is not None:
        reason = over_limit(estimate, limit)
    if reason is None and metered and (estimate is None or estimate.confidence == "unknown"):
        reason = "engine tarifada sem estimativa de custo"
    return CostCheck(estimate=estimate, plan=plan, reason=reason)
