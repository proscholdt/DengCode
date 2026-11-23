# ADR 0004 — Preset open source primeiro, nuvem depois

- Status: aceito
- Data: 2026-11-23

## Contexto
Desenvolver contra nuvens desde o início exige contas, custos e torna os
testes lentos e frágeis.

## Decisão
As fases 2–5 usam só componentes locais (DuckDB, Postgres, MinIO, Trino,
Airflow, Kafka em Docker). Adapters de nuvem só entram na fase 7, quando as
portas e as suítes de contrato já estiverem estáveis.

## Consequências
- (+) Loop de desenvolvimento rápido e gratuito; CI sem credenciais.
- (−) Problemas específicos de nuvem aparecem tarde → mitigado pelas capability
  flags definidas desde a fase 1.
