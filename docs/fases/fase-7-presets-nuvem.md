# Fase 7 — Presets de nuvem (Azure → AWS → Google)

**Objetivo:** os presets `azure`, `aws` e `google` funcionando contra contas
reais de teste, com os mesmos contratos das fases anteriores.

**Regra:** implemente um provedor por vez, na ordem Azure → AWS → Google.
Cada adapter: pacote próprio, contrato do testkit, testes com respostas
gravadas no CI normal e contra conta real num workflow noturno opcional
(`ci-cloud.yml`, só roda se os secrets existirem).

## Azure

| Porta | Adapter | Notas |
| --- | --- | --- |
| LLM | `azure_openai` (reusar `llm-openai`) | deployment, api-version, Entra ID ou chave |
| Storage | `adls_gen2` | `abfss://`; `DefaultAzureCredential` |
| QueryEngine/Catalog | `databricks` | SQL Warehouse via `databricks-sql-connector`; Unity Catalog via SDK; extras: listar clusters/jobs |
| QueryEngine | `synapse` | T-SQL (`mssql`/`pyodbc` ou `mssql-python`); serverless vs dedicated nas capabilities |
| Orchestrator | `adf` | pipelines, runs, activity runs + erros; definição JSON; `supports_code_definitions=False` |
| Catalog | `unity_catalog` | lineage via API do Unity quando disponível |
| Stream | `event_hubs` | protocolo Kafka (reusar `kafka` com auth SASL/OAuth) ou SDK nativo |
| StreamCompute | `databricks_streaming`, `stream_analytics` | status, atraso, logs |
| Secrets | `key_vault` | |

## AWS

| Porta | Adapter | Notas |
| --- | --- | --- |
| LLM | `bedrock` | Converse API com tools |
| Storage | `s3` | reusar núcleo do `minio` |
| QueryEngine | `athena` | `metered=True`; custo por bytes escaneados (estatística da execução + estimativa); workgroup |
| QueryEngine | `emr_spark` | opcional |
| Catalog | `glue_catalog` | |
| Orchestrator | `airflow` variante `mwaa` | só autenticação muda |
| Stream | `kinesis`, `msk` (reusar `kafka`) | |
| Secrets | `secrets_manager` | |

## Google

| Porta | Adapter | Notas |
| --- | --- | --- |
| LLM | `vertex_ai`, `gemini` | |
| Storage | `gcs` | |
| QueryEngine | `bigquery` | `supports_dry_run=True` (bytes processados) |
| Catalog | `dataplex` (+ INFORMATION_SCHEMA do BigQuery) | |
| Orchestrator | `airflow` variante `composer` | |
| Stream | `pubsub` | |
| StreamCompute | `dataflow` | |
| Secrets | `gcp_secret_manager` | |

## Para cada provedor

- [ ] pacote de conhecimento de cada ferramenta (boas práticas, armadilhas, limites, custo)
- [ ] extras de instalação (`dengcode[azure]`, `dengcode[aws]`, `dengcode[google]`)
- [ ] preset em `stacks/` revisado e validado
- [ ] guia `docs/guias/<provedor>.md`: credenciais, permissões mínimas (least privilege), `stack doctor`

## Critério de pronto

- [ ] Cada preset passa nos contratos e no `stack doctor` contra uma conta de teste.
- [ ] Um cenário de ponta a ponta por provedor (ex.: Azure — investigar falha de
      pipeline ADF que chama notebook Databricks).
- [ ] CI normal verde sem credenciais (respostas gravadas).
