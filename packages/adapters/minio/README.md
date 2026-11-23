# minio

Adapter de `StoragePort` sobre a API S3, para MinIO e endpoints compatíveis
(entry point `minio`). Usa o [`obstore`](https://pypi.org/project/obstore/)
(núcleo em Rust, API assíncrona, wheels para Windows, Linux e macOS).

## Configuração

| Chave | Padrão | Descrição |
| --- | --- | --- |
| `endpoint` | — (obrigatória) | `http://localhost:9000`; `http://` libera tráfego sem TLS |
| `region` | `us-east-1` | região assinada nas requisições |
| `access_key` / `secret_key` | — | já resolvidas pelo motor (`env://...`); sem elas o acesso é anônimo |
| `bucket` | — | bucket padrão para URIs vazias (`""` ou `"."`) |
| `allow_write` | `false` | sem ela, `write_bytes` levanta `UnsupportedOperation` |
| `timeout_s` | `30` | tempo máximo por requisição (inclui novas tentativas) |

## URIs

- `s3://bucket/chave` (ou `bucket/chave`): o primeiro segmento é sempre o bucket.
- `ObjectInfo.uri` é sempre `s3://bucket/chave`; prefixos terminam em `/` e
  vêm com `is_dir=True` na listagem não recursiva.
- `read_sample`/`infer_schema` aceitam um prefixo: usam o primeiro objeto
  tabular (CSV/TSV, Parquet, JSON por linha) em ordem lexicográfica.
- CSV/JSON: baixa no máximo 32 MB e descarta a última linha parcial.
  Parquet maior que isso é lido por faixas (rodapé + primeiro grupo de linhas).

## Erros

Objeto/bucket inexistente → `NotFoundError`; credencial → `AuthError`;
rede, timeout e 5xx → `TransientError` (`SlowDown`/429 → `RateLimitError`);
o resto → `AdapterError`. Credenciais são removidas das mensagens.

## Testes

```
uv run pytest packages/adapters/minio -m "not integration"   # rápidos
uv run pytest packages/adapters/minio -m integration         # MinIO via testcontainers
```
