"""PermissionPolicy: decide ALLOW / ASK / DENY para cada ação do agente.

Regras (sqlguard.md, seção "Arquivos e shell"):
- escrita fora do workspace = DENY;
- shell: comandos destrutivos conhecidos = DENY; allowlist = ALLOW; resto = ASK;
- modo ``read_only`` bloqueia qualquer escrita; ``auto`` libera escrita local só em dev;
- prod nunca escreve remotamente, a menos que ``allow_prod_writes``.
O conteúdo vindo de dados/arquivos nunca altera a política: ela só olha a ação.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from dengcode_core.tools.base import ActionRequest, Risk


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionMode(StrEnum):
    ASK = "ask"
    AUTO = "auto"
    READ_ONLY = "read_only"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    warnings: tuple[str, ...] = ()


DEFAULT_SHELL_ALLOWLIST: tuple[str, ...] = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "pytest",
    "python -m pytest",
    "uv run pytest",
    "uv run ruff",
    "ruff",
    "dbt compile",
    "dbt ls",
    "dbt test",
    "dbt parse",
    "uv run dbt compile",
    "uv run dbt ls",
    "uv run dbt test",
    "uv run dbt parse",
    "ls",
    "dir",
    "pwd",
    "echo",
)

_DESTRUCTIVE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+(-\w*\s+)*-\w*[rf]\w*",  # rm -rf, rm -r, rm -f...
        r"\bdel\s+(/\w\s+)*/[sq]\b",
        r"\b(rd|rmdir)\s+(/\w\s+)*/s\b",
        r"\bformat(\.com)?\s+[a-z]:",
        r"\bmkfs(\.\w+)?\b",
        r"\bdd\s+if=",
        r":\(\)\s*\{",
        r"\b(shutdown|reboot|halt)\b",
        r"\bgit\s+push\s+.*--force",
        r"\bgit\s+reset\s+--hard",
        r"\bgit\s+clean\s+-\w*f",
        r"Remove-Item\s+.*-Recurse",
        r">\s*/dev/sd[a-z]",
    )
]
_CHAIN = re.compile(r"&&|\|\||;|\||`|\$\(|>|<|\n")


def is_destructive(command: str) -> bool:
    return any(p.search(command) for p in _DESTRUCTIVE)


def is_allowlisted(command: str, allowlist: Iterable[str]) -> bool:
    """Todo segmento do comando precisa começar por um prefixo permitido."""
    if _CHAIN.search(command):
        segments = [s.strip() for s in re.split(r"&&|\|\||;|\|", command)]
        if any(_CHAIN.search(s) for s in segments):  # redireção, subshell...
            return False
    else:
        segments = [command.strip()]
    prefixes = [a.strip() for a in allowlist if a.strip()]
    return all(
        any(seg == p or seg.startswith(p + " ") for p in prefixes) for seg in segments if seg
    )


@dataclass
class PermissionPolicy:
    mode: PermissionMode = PermissionMode.ASK
    env: str = "dev"
    shell_allowlist: tuple[str, ...] = DEFAULT_SHELL_ALLOWLIST
    protected_tables: tuple[str, ...] = ()
    allow_prod_writes: bool = False
    grants: set[str] = field(default_factory=lambda: set[str]())

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def grant(self, request: ActionRequest) -> None:
        """ "Sempre nesta sessão" — nunca transforma um DENY em ALLOW."""
        self.grants.add(request.grant_key)

    def _ask(self, request: ActionRequest, reason: str, *warnings: str) -> PolicyDecision:
        if request.grant_key in self.grants and not warnings:
            return PolicyDecision(Decision.ALLOW, f"{reason} (liberado nesta sessão)")
        return PolicyDecision(Decision.ASK, reason, tuple(warnings))

    def decide(self, request: ActionRequest, *, inside: bool = True) -> PolicyDecision:
        """``inside``: todos os caminhos da ação estão dentro do workspace."""
        risk = request.risk
        if risk is Risk.READ:
            if not inside:
                return self._ask(request, "leitura fora do workspace")
            return PolicyDecision(Decision.ALLOW, "leitura")

        if risk is Risk.WRITE_LOCAL:
            if not inside:
                return PolicyDecision(Decision.DENY, "escrita fora do workspace")
            if self.mode is PermissionMode.READ_ONLY:
                return PolicyDecision(Decision.DENY, "sessão somente leitura")
            if self.mode is PermissionMode.AUTO and not self.is_prod:
                return PolicyDecision(Decision.ALLOW, "modo auto em dev")
            return self._ask(request, "escrita de arquivo")

        if risk is Risk.EXECUTE:
            command = request.command or ""
            if is_destructive(command):
                return PolicyDecision(Decision.DENY, "comando destrutivo bloqueado")
            if is_allowlisted(command, self.shell_allowlist):
                return PolicyDecision(Decision.ALLOW, "comando na lista permitida")
            if self.mode is PermissionMode.READ_ONLY:
                return PolicyDecision(
                    Decision.DENY, "sessão somente leitura: comando fora da lista"
                )
            return self._ask(request, "comando fora da lista permitida")

        if risk is Risk.WRITE_REMOTE:
            if self.mode is PermissionMode.READ_ONLY:
                return PolicyDecision(Decision.DENY, "sessão somente leitura")
            if self.is_prod and not self.allow_prod_writes:
                return PolicyDecision(Decision.DENY, "escrita remota em prod bloqueada")
            if self.mode is PermissionMode.AUTO and not self.is_prod:
                return PolicyDecision(Decision.ALLOW, "modo auto em dev")
            return self._ask(request, "escrita remota")

        # Risk.SQL é decidido por decide_sql (fase 3); por segurança, pergunta.
        return self._ask(request, f"ação de risco {risk.value}")
