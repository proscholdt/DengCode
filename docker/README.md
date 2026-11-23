# Stack open source local (`docker/compose.oss.yaml`)

Serviços usados pelo preset open source na fase 3: um **Postgres 16** e um
**MinIO** (storage compatível com S3), com um bucket de exemplo.

> **Somente para desenvolvimento.** As credenciais abaixo são públicas e fixas.
> Não use esta stack em produção e não exponha as portas para a rede.

## Subir e derrubar

```
make up-oss     # docker compose -f docker/compose.oss.yaml up -d
make down-oss   # derruba os containers; os volumes (dados) continuam
```

Para apagar também os dados (volumes nomeados):

```
docker compose -f docker/compose.oss.yaml down -v
```

Funciona igual em Windows (Docker Desktop), Linux e macOS: não há script de
shell no host, só o compose.

## Serviços, portas e credenciais (dev-only)

| Serviço | Porta no host | Credenciais | Observação |
| --- | --- | --- | --- |
| `postgres` | `5432` | banco `dengcode`, usuário `dengcode`, senha `dengcode` | volume `postgres-data` |
| `minio` (API S3) | `9000` | access key `dengcode`, secret key `dengcode123` | volume `minio-data` |
| `minio` (console web) | `9001` | as mesmas do MinIO | http://localhost:9001 |
| `minio-init` | — | — | execução única, termina com código 0 |
| `iceberg-rest` (`oss/trino.yaml`) | `8181` | — | catálogo Iceberg REST; warehouse em `s3://dengcode-exemplo/warehouse/` |
| `trino` (`oss/trino.yaml`) | `8080` | usuário `dengcode`, sem senha | catálogos `iceberg` e `memory`; heap 1 GB |
| `airflow` (`oss/airflow.yaml`) | `8081` | `admin` / `admin` | `airflow standalone` (SQLite); DAGs `carga_pedidos` (falha de propósito) e `relatorio_diario` |
| `kafka` (`oss/kafka.yaml`) | `9092` | — | KRaft, um nó; tópico `pedidos` (3 partições) |
| `kafka-produtor` | — | — | execução única: cria `pedidos`, publica 50 eventos sintéticos e deixa o grupo `servico-faturamento` com lag |

O arquivo principal inclui os fragmentos de `docker/oss/` (`include:`); para
subir só uma parte, use o fragmento direto, ex.:
`docker compose -f docker/oss/kafka.yaml up -d`.

O `minio-init` cria o bucket `dengcode-exemplo` e copia os CSVs de
`examples/ecommerce/` (se existirem) para `s3://dengcode-exemplo/ecommerce/`.
A pasta é montada somente leitura; se estiver ausente ou vazia, o bucket é
criado vazio e nada falha. Para republicar depois de mudar os CSVs:

```
docker compose -f docker/compose.oss.yaml run --rm minio-init
```

Os dois serviços têm *healthcheck* (`pg_isready` e `mc ready local`); use
`docker compose -f docker/compose.oss.yaml ps` para ver o estado.

### Exemplo de configuração na stack

Credenciais nunca vão no YAML: aponte para variáveis de ambiente (ou `.env`)
com `env://`, por exemplo `DENGCODE_MINIO_ACCESS_KEY=dengcode` e
`DENGCODE_MINIO_SECRET_KEY=dengcode123`.

```yaml
storage:
  adapter: minio
  endpoint: http://localhost:9000
  access_key: env://DENGCODE_MINIO_ACCESS_KEY
  secret_key: env://DENGCODE_MINIO_SECRET_KEY
  bucket: dengcode-exemplo
```

## Variáveis opcionais

Para evitar conflito de portas ou trocar as imagens:

| Variável | Padrão |
| --- | --- |
| `DENGCODE_POSTGRES_PORT` | `5432` |
| `DENGCODE_MINIO_PORT` | `9000` |
| `DENGCODE_MINIO_CONSOLE_PORT` | `9001` |
| `DENGCODE_POSTGRES_IMAGE` | `postgres:16-alpine` |
| `DENGCODE_MINIO_IMAGE` | `pgsty/minio:RELEASE.2026-08-04T00-00-00Z` |
| `DENGCODE_MC_IMAGE` | `pgsty/mc:RELEASE.2026-10-26T00-00-00Z` |
| `DENGCODE_TRINO_PORT` / `DENGCODE_ICEBERG_REST_PORT` | `8080` / `8181` |
| `DENGCODE_AIRFLOW_PORT` / `DENGCODE_AIRFLOW_IMAGE` | `8081` / `apache/airflow:slim-3.3.2` |
| `DENGCODE_KAFKA_PORT` / `DENGCODE_KAFKA_IMAGE` | `9092` / `apache/kafka:3.9.1` |

A MinIO deixou de publicar imagens oficiais da edição comunitária
(`minio/minio` não está mais disponível no Docker Hub); por isso o padrão é o
fork comunitário `pgsty/minio`, que mantém o mesmo binário e a mesma API S3.
Se a sua empresa tiver um espelho próprio, aponte as variáveis para ele.

## Recursos necessários

- RAM medida (repouso → em uso): Postgres ~100 MB, MinIO ~250 MB,
  Iceberg REST ~100 MB, Trino ~0,8 → 1,0 GB, Airflow ~0,85 → 1,05 GB,
  Kafka ~350 MB. **Stack completa: reserve 4–5 GB** para o Docker (no Docker
  Desktop, 6 GB ou mais alocados à VM). Só Postgres + MinIO: ~1 GB.
- Disco: ~3 GB de imagens (Trino e Airflow são as maiores), mais os volumes.
- O Airflow leva ~1 minuto para ficar saudável.
