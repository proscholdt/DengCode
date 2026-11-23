# Fase 5 — Memória, orquestração e streaming (preset open source completo)

**Objetivo:** o agente lembra do ambiente, investiga falhas de pipeline e
enxerga streaming. O preset `opensource` fica completo.

**Leia antes:** `docs/especificacoes/memoria.md`, `portas.md` (Orchestrator, Stream).

## Tarefas

1. **Workspaces** — `dengcode workspace new|list|use`; tudo em
   `~/.dengcode/workspaces/<ws>/`; escopo obrigatório propagado por toda a sessão.
2. **MemoryStorePort** + adapter `sqlite` (FTS5 + sqlite-vec + arestas), busca
   híbrida (RRF), fallback sem vetores.
3. **EmbeddingPort** + adapter local (ex.: `fastembed` ou `sentence-transformers`,
   escolha o mais leve e multiplataforma; registre ADR) e adapters dos provedores
   da fase 2 que oferecem embeddings.
4. **Sincronização de catálogo** — `dengcode catalog sync`: grava `CATALOG_TABLE`
   e `COLUMN_STATS` com `schema_hash`; detecção de `stale` ao consultar.
5. **Ferramentas** `memory_search`, `memory_propose` (cria `proposed`; CLI pede
   confirmação), e injeção do resumo do workspace no prompt.
6. **PROJETO.md** — `dengcode init` cria `.dengcode/PROJETO.md` com modelo
   (convenções, camadas, tabelas protegidas); o loop carrega no prompt;
   `protected_tables` alimenta a política.
7. **Episódica** — ao fim de uma tarefa de investigação, o agente propõe um
   `INCIDENT` (sintoma, causa, solução); antes de investigar falhas, busca
   incidentes parecidos.
8. **OrchestratorPort** + adapter `airflow` (API REST estável do Airflow, com
   variantes de autenticação: básica/local; `mwaa` e `composer` ficam para a fase 7).
   Ferramentas: `pipeline_list`, `pipeline_runs`, `pipeline_run_detail`,
   `pipeline_task_logs`, `pipeline_trigger` (risco: escrita remota).
9. **StreamPort** + adapter `kafka` (`aiokafka`/`confluent-kafka`): tópicos,
   amostra (mascarada), lag de consumidor, schema (se houver registry).
10. **StreamComputePort** — interface + fake; adapter real fica para a fase 7
    (Flink/Spark streaming opcional aqui).
11. **Compose completo** — Postgres, MinIO, Trino (catálogo Iceberg REST), Airflow
    (com 2 DAGs de exemplo, uma que falha de propósito), Kafka (+ produtor de
    exemplo). Documentar requisitos de memória RAM.
12. **Adapter `trino`** (QueryEngine + Catalog) e **`iceberg_rest`** (Catalog).
13. **Pacotes de conhecimento** para airflow, kafka, trino, iceberg + fundamentos
    de streaming (exactly-once, watermarks, janelas, checkpointing, late data).

## Critério de pronto

- [ ] "Por que a DAG `carga_pedidos` falhou ontem?" → o agente lê runs e logs,
      explica a causa e propõe correção; na segunda vez, cita o incidente salvo.
- [ ] Dois workspaces com dados diferentes: busca em um nunca retorna registro do outro (teste).
- [ ] Preset `opensource` completo passa em `stack doctor` com `make up-oss`.
- [ ] Todos os contratos passam para os adapters open source.
- [ ] CI verde.
