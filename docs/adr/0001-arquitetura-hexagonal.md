# ADR 0001 — Arquitetura hexagonal com portas por conceito

- Status: aceito
- Data: 2026-11-23

## Contexto
O sistema precisa funcionar com qualquer combinação de ferramentas de dados
(ADF + Databricks + Blob + Synapse, Athena + Airflow + S3, BigQuery, open source)
e com qualquer LLM, sem ficar preso a um fornecedor.

## Decisão
Arquitetura hexagonal. O core define **portas por conceito de engenharia de
dados** (Storage, QueryEngine, Catalog, Orchestrator, Stream…), nunca por
ferramenta. Ferramentas e provedores são adapters em pacotes separados,
descobertos por entry points. Regras de dependência verificadas com import-linter.

## Consequências
- (+) Trocar de stack é configuração; terceiros criam adapters sem tocar no core.
- (−) Risco de mínimo denominador comum → mitigado por capability flags e
  ferramentas extras por adapter.
