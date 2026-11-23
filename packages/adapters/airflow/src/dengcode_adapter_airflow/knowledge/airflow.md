# Airflow — guia para investigar e corrigir pipelines

## Conceitos

- **DAG**: o pipeline, definido em código Python (`definition_path` aponta o arquivo).
  Tem `schedule` (cron, preset como `@daily`, intervalo, dataset/asset ou nenhum),
  `start_date`, `catchup` e pode estar **pausada** (não cria runs agendados).
- **DAG run**: uma execução da DAG. No DengCode o id é `"<dag_id>/<dag_run_id>"`
  (ex.: `carga_pedidos/scheduled__2026-11-23T00:00:00+00:00`). O prefixo do
  `dag_run_id` diz a origem: `scheduled__` (agenda), `manual__` (disparo manual/API),
  `backfill__`, `asset_triggered__`/`dataset_triggered__`.
- **Task / task instance**: cada passo da DAG dentro de um run. Estados:
  `success`, `failed`, `upstream_failed` (não rodou porque uma task anterior falhou),
  `skipped` (pulada por branch/short-circuit), `up_for_retry` (falhou e vai tentar
  de novo), `up_for_reschedule` (sensor em modo reschedule), `deferred` (esperando
  um trigger assíncrono), `queued`/`scheduled` (aguardando slot).
- **Try (tentativa)**: cada execução de uma task instance; `try_number` cresce a
  cada retry. O log é por tentativa — o adapter devolve o da **última**.
- **Logical date / data interval**: a "data de referência" do run (o período que
  ele processa), não a hora em que rodou. Um run `@daily` de 2026-11-22 processa
  o dia 22 e roda na virada para o dia 23.

## Retries

- `retries` e `retry_delay` (e `retry_exponential_backoff`) em `default_args` ou
  na task. Use retry para falhas **temporárias** (rede, API fora, lock no banco).
- Não adianta retry para erro determinístico (schema, dado inválido, bug): só
  atrasa o alerta. Nesses casos, `retries=0` na task de validação.
- `execution_timeout` evita task travada para sempre; `sla`/alertas avisam atraso.

## Sensors

- Esperam uma condição (arquivo chegar, partição existir, outra DAG terminar).
- `mode="poke"` ocupa um worker o tempo todo; prefira `mode="reschedule"` ou
  sensors **deferrable** (liberam o worker e ficam `deferred`).
- Sempre defina `timeout` e `poke_interval`. Sensor que estoura `timeout` falha
  com `AirflowSensorTimeout` — geralmente a causa real é o dado não ter chegado
  (olhe o pipeline anterior / a origem, não o sensor).

## Tasks idempotentes

- Rodar a mesma task duas vezes para o mesmo intervalo deve dar o **mesmo
  resultado**: escreva particionado pela logical date e use sobrescrita da
  partição (`INSERT OVERWRITE`, `MERGE`, `DELETE` + `INSERT` na mesma transação),
  nunca `INSERT` puro de append.
- Use `{{ ds }}`/`data_interval_start` em vez de `now()`: o reprocessamento de um
  dia antigo precisa ler e gravar aquele dia.
- Não passe dados grandes por XCom: grave em storage e passe o caminho.

## Backfill e catchup

- `catchup=True` cria runs para todos os intervalos desde `start_date` que ainda
  não rodaram (útil para histórico, perigoso ao despausar uma DAG antiga).
- Backfill reprocessa um período de propósito (`airflow backfill create` no
  Airflow 3; `airflow dags backfill` no 2). Só é seguro se as tasks forem idempotentes.
- Limite o paralelismo (`max_active_runs`) para não sobrecarregar a origem.
- Pelo DengCode, backfill ainda não é suportado (`supports_backfill=False`);
  reprocessar um dia = `pipeline_trigger` com `conf` (escrita remota, pede aprovação).

## Como diagnosticar uma falha (roteiro)

1. `pipeline_runs` da DAG: o run falhou uma vez ou falha sempre? Desde quando?
   Runs anteriores com sucesso indicam mudança recente (código, dado, ambiente).
2. `pipeline_run_detail` do run com falha: `failed_tasks` lista as tasks em
   `failed` e `upstream_failed`. **Comece pela primeira `failed`** (as
   `upstream_failed` só herdaram a falha e não têm log útil).
3. `pipeline_task_logs` da task que falhou: vá ao fim do log. Procure a linha
   `ERROR - Task failed with exception`, o `Traceback` e a **última linha da
   exceção** (`ValueError: ...`, `KeyError: ...`). Linhas `::group::` são só
   marcadores de seção da UI.
4. Classifique a causa e **confirme com dados** (consulte a tabela de origem,
   conte nulos, compare schemas) antes de propor a correção.
5. Proponha a correção na origem do problema e a forma segura de reprocessar.

## Causas comuns e como corrigir

| Sintoma no log | Causa provável | Correção |
| --- | --- | --- |
| `falha de qualidade`, contagem de nulos/duplicados | dado de origem mudou, join quebrado, carga parcial | investigar a origem e o join; corrigir o dado ou a regra; reprocessar a partição |
| `KeyError`/coluna inexistente, `UndefinedColumn` | schema da origem mudou | ajustar o mapeamento/modelo; contrato de schema na origem |
| `Connection refused`, `timeout`, `503` | serviço externo fora ou lento | retry com backoff; `execution_timeout`; verificar a conexão (`Connection` no Airflow) |
| `AirflowSensorTimeout` | dado de entrada não chegou | olhar o pipeline/processo anterior; ajustar janela do sensor |
| `Negsignal.SIGKILL`, `-9`, `zombie` | falta de memória ou worker morto | processar em lotes, aumentar recursos, evitar carregar tudo em memória |
| `ModuleNotFoundError`, DAG some da lista | erro de import/parse do arquivo da DAG | corrigir dependência/código; ver erros de import do Airflow |
| `permission denied`, `401`/`403` | credencial expirada ou sem permissão | renovar o segredo/conexão; revisar o papel |
| `duplicate key`, dados dobrados após retry | task não idempotente | trocar append por overwrite/merge da partição |
| task fica `queued` muito tempo | pool/slots cheios, `max_active_tasks` | revisar pools e concorrência |

## Boas práticas ao propor mudanças

- Mudança em DAG é mudança de código: edite o arquivo de `definition_path`,
  mantenha `catchup` e `start_date` estáveis (alterar `start_date` cria runs novos).
- Valide antes/depois com dados (contagens, nulos, diff da partição reprocessada).
- Disparar runs, limpar tasks ou fazer backfill altera o ambiente: sempre com
  aprovação, começando por um único intervalo.
