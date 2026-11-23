# ADR 0005 — Seguro por padrão (SqlGuard, prod somente leitura, PII mascarada)

- Status: aceito
- Data: 2026-11-23

## Contexto
O agente toca dados de empresas, muitas vezes de clientes diferentes, e pode
executar operações caras ou destrutivas.

## Decisão
- Todo SQL passa pelo SqlGuard (sqlglot) antes de qualquer engine.
- Prod é somente leitura por padrão; escrita exige configuração explícita.
- PII é mascarada antes de chegar ao LLM; memória persistente guarda só metadados.
- Memória e caches são isolados por workspace.
- Toda execução é auditada.

## Consequências
- (+) Confiança para usar em ambientes reais.
- (−) Mais pedidos de aprovação → modos `auto` (só dev) e regras por projeto.
