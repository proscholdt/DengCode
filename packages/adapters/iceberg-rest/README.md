# dengcode-adapter-iceberg-rest

CatalogPort (só leitura) sobre qualquer catálogo que implemente a spec
**Iceberg REST Catalog** (servidor de referência, Polaris, Lakekeeper, Nessie,
Gravitino...), falando HTTP direto com `httpx2`.

Entry point (grupo `dengcode.adapters`): `iceberg_rest`. Pacote de conhecimento:
`dengcode_adapter_iceberg_rest:knowledge/iceberg.md`.

## Configuração

```yaml
catalog:
  adapter: iceberg_rest
  url: http://localhost:8181     # obrigatória (sem o /v1)
  warehouse: s3://lago/          # opcional; vai em GET /v1/config?warehouse=
  prefix: lago                   # opcional; senão vem do /v1/config (overrides.prefix)
  token: env://ICEBERG_TOKEN     # opcional; Authorization: Bearer
  credential: env://ICEBERG_CRED # opcional; "client_id:client_secret" (OAuth2)
  oauth2_server_uri: https://...  # opcional; padrão <url>/v1/oauth/tokens
  scope: catalog                 # opcional; escopo OAuth2 (padrão "catalog")
  catalog_name: iceberg          # opcional; nome devolvido em TableRef.catalog
  timeout_s: 30                  # opcional; por requisição
```

`token` e `credential` nunca vão para log, `repr` nem mensagem de erro. Com
`credential`, o token é obtido por *client credentials* e renovado uma vez se o
servidor responder 401.

## Por que HTTP direto e não `pyiceberg`

O adapter só lê três rotas (`/v1/config`, namespaces, tabelas) e o JSON de
metadados. O `pyiceberg` traria leitores de arquivo, FileIO e dependências
nativas que o catálogo nunca usa; o `httpx2` já está no workspace.

## Comportamento

- `list_schemas` = todos os namespaces, inclusive aninhados (`vendas.bronze`),
  com paginação (`next-page-token`). Na URL, níveis vão separados por `%1F`
  (ou pelo `namespace-separator` anunciado no `/v1/config`).
- `list_tables(schema)` = tabelas do namespace (ordenadas por nome).
- `describe` = schema **atual** dos metadados (`current-schema-id`; v1 usa
  `schema`): `required` → `nullable=False`, `doc` → descrição, tipos aninhados
  como `list<string>`, `map<string, long>`, `struct<a: int>`;
  `table_format="iceberg"`, `location`, `description` = `properties.comment`,
  `row_count` = `total-records` do snapshot atual (sem snapshot → `0`; snapshot
  sem o resumo → `None`) e `schema_hash`.
- `TableRef.catalog` é ignorado na busca (um adapter = um catálogo REST); nas
  respostas vem o `catalog_name` da config — use o mesmo nome do catálogo no
  motor SQL (ex.: `iceberg` no Trino) para o agente consultar a tabela direto.
- Nomes de namespace com `.` dentro de um nível ficam ambíguos no formato com
  ponto; evite-os.
- `column_stats` → `UnsupportedOperation` (`supports_column_stats=False`); use o
  motor de consulta. `lineage` → `UnsupportedOperation`.

### Erros

| HTTP / situação | Erro de domínio |
| --- | --- |
| 404 (`NoSuchTableException`, `NoSuchNamespaceException`) | `NotFoundError` |
| 401, 419 | `AuthError` |
| 403 | `PermissionDeniedError` |
| 429 | `RateLimitError` |
| 5xx, conexão recusada, timeout | `TransientError` |
| demais (400, JSON inválido...) | `AdapterError` |

A mensagem leva `message` e `type` do `ErrorModel` da spec, nunca o stack trace
do servidor.

## Testes

```
uv run pytest packages/adapters/iceberg-rest -m "not integration"   # catálogo de mentira
uv run pytest packages/adapters/iceberg-rest -m integration         # apache/iceberg-rest-fixture:1.10.1
```

A integração cria namespaces e tabelas pela própria API REST; para ter
`total-records`, um commit adiciona um snapshot sintético (o catálogo não lê a
*manifest list*). Sem Docker, os testes de integração são pulados.
