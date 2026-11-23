# dengcode-adapter-airflow

`OrchestratorPort` sobre a **API REST estável do Airflow**, com `httpx2` assíncrono.
Funciona com **Airflow 3** (`/api/v2`, token JWT) e **Airflow 2** (`/api/v1`,
autenticação HTTP básica).

Entry point (grupo `dengcode.adapters`): `airflow`.

## Configuração

```yaml
orchestrator:
  adapter: airflow
  url: http://localhost:8081      # endereço da UI/API (pode ter prefixo, ex.: /airflow)
  username: env://DENGCODE_AIRFLOW_USER
  password: env://DENGCODE_AIRFLOW_PASSWORD
  # api_version: 3                # 2 | 3; padrão: detectar
  # variant: local                # local | basic
  # timeout_s: 30
  # verify_tls: true
  # max_attempts: 3               # tentativas dos GETs em erro temporário
```

| Chave | Descrição |
| --- | --- |
| `url` | obrigatória; endereço base do Airflow |
| `username`, `password` | já resolvidos pelo motor; nunca vão para log nem para mensagens de erro |
| `api_version` | `2` (Airflow 2, `/api/v1`) ou `3` (Airflow 3, `/api/v2`). Sem valor (ou `auto`): sonda `/api/v2/version` e depois `/api/v1/version` |
| `variant` | `local` (padrão): JWT de `POST /auth/token` no Airflow 3, básica no 2; sem usuário, nenhuma autenticação. `basic`: sempre HTTP básica (ex.: proxy na frente) |
| `timeout_s` | tempo máximo por requisição (padrão 30) |
| `verify_tls` | valida o certificado HTTPS (padrão `true`) |

`mwaa` e `composer` ficam para a fase 7: entram como novas estratégias em
`client.AUTH_VARIANTS` (hoje dão `ConfigError` com aviso).

## Identificadores

- **pipeline** = `dag_id`.
- **run** = `"<dag_id>/<dag_run_id>"` (a API precisa dos dois; o `dag_id` nunca
  tem `/`). É o formato devolvido por `list_runs`/`trigger` e aceito por
  `get_run` e `task_logs`. Ex.: `carga_pedidos/manual__2026-11-23T10:00:00+00:00`.
- **task** = `task_id`; tasks mapeadas (dynamic task mapping) aparecem como
  `"<task_id>[<map_index>]"`, aceito por `task_logs`.

## Comportamento

- `list_pipelines`: DAGs ativas (paginado). `schedule` vem de `timetable_summary`
  (Airflow 3) ou `schedule_interval` (Airflow 2); `definition_path` de
  `relative_fileloc`/`fileloc`.
- `list_runs`: mais recentes primeiro. É um resumo: as task instances só são
  buscadas para runs com falha (`failed_tasks` e `tasks` preenchidos); nos demais
  `tasks` fica vazio — use `get_run` para o detalhe. DAG inexistente → `NotFoundError`.
- `get_run`: run + todas as task instances.
- `task_logs`: texto da **última tentativa** (eventos JSON do Airflow 3 viram linhas
  de texto, com traceback; o Airflow 2 já devolve texto), cortado nas últimas
  `tail_lines` linhas. Task que não chegou a executar (ex.: `upstream_failed`)
  devolve uma explicação em vez de log.
- `trigger`: cria um run com `conf = params`. **Escrita remota** — o motor pede
  aprovação. Nunca é repetido automaticamente.

Mapeamento de estados:

| Airflow | `RunStatus` |
| --- | --- |
| `success` | `success` |
| `failed`, `upstream_failed` | `failed` (entram em `failed_tasks`) |
| `running`, `up_for_retry`, `up_for_reschedule`, `deferred`, `restarting` | `running` |
| `queued`, `scheduled`, sem estado | `queued` |
| `skipped`, `removed` | `cancelled` |
| outro | `unknown` |

Erros: 401/403 → `AuthError`; 404 → `NotFoundError`; 429 → `RateLimitError`;
5xx, tempo esgotado e falha de conexão → `TransientError` (com `Retry-After`);
demais → `AdapterError`. Só os GETs são repetidos (`call_with_retry`).

## Testes

```
uv run pytest packages/adapters/airflow -m "not integration"   # respostas gravadas
uv run pytest packages/adapters/airflow -m integration         # Airflow 3 real (Docker)
```

O teste de integração sobe `docker/oss/airflow.yaml` (Airflow 3.3 `standalone`;
~0,85 GB de RAM em repouso, ~1,05 GB executando tarefas) num projeto compose
próprio e numa porta livre, dispara `carga_pedidos`, espera a falha, roda o
contrato e derruba tudo com `down -v` (~1 min no total).
Sem Docker, é pulado.
