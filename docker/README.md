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

A MinIO deixou de publicar imagens oficiais da edição comunitária
(`minio/minio` não está mais disponível no Docker Hub); por isso o padrão é o
fork comunitário `pgsty/minio`, que mantém o mesmo binário e a mesma API S3.
Se a sua empresa tiver um espelho próprio, aponte as variáveis para ele.

## Recursos necessários

- RAM: cerca de **1 GB livre** para o Docker (Postgres ~100 MB e MinIO
  ~200–300 MB em repouso, com folga para consultas). No Docker Desktop,
  mantenha pelo menos 2 GB alocados à VM.
- Disco: ~600 MB de imagens, mais o que for gravado nos volumes.
