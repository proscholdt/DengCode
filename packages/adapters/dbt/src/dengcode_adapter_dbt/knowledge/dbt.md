# dbt — pacote de conhecimento

Ferramenta de transformação: modelos `SELECT` em SQL (com Jinja) versionados no
repositório; o dbt resolve dependências, gera o DDL/DML e materializa no
warehouse. Vale para dbt Core 1.8+ (notas de versões mais novas indicadas).
No DengCode, o dbt é acessado pela `TransformPort` (`list_models`, `compile`,
`run`, `test`, `manifest_lineage`).

## Layout do projeto

```
dbt_project.yml          # nome, profile, paths, configs por pasta (+materialized, +schema, +tags)
profiles.yml             # conexões por target (dev, prod); fica em ~/.dbt/ ou via --profiles-dir
models/
  staging/  (ou bronze/silver)   # 1:1 com fontes, limpeza leve
  intermediate/
  marts/    (ou gold)
  */_schema.yml / *.yml  # sources, descrição de modelos/colunas, testes
macros/  seeds/  snapshots/  tests/ (testes singulares .sql)  analyses/
target/                  # artefatos gerados (não versionar)
packages.yml             # dependências (dbt deps)
```

- O nome do arquivo `.sql` é o nome do modelo (e da relação, salvo `alias`).
- Schema final = `generate_schema_name`: por padrão `<schema do target>_<+schema>`
  (ex.: `dev_joao_silver`). Projetos costumam sobrescrever a macro; confira
  antes de supor onde a tabela foi criada (`relation` no manifest).

## `ref` e `source`

```sql
SELECT p.pedido_id, p.cliente_id, c.segmento
FROM {{ ref('silver_pedidos') }} p
JOIN {{ ref('silver_clientes') }} c USING (cliente_id)
-- fontes externas declaradas em YAML:
-- FROM {{ source('erp', 'pedidos') }}
```

- **Nunca** escreva o nome físico da tabela no lugar de `ref`/`source`: quebra o
  DAG, o lineage, a seleção e a troca de ambiente (dev/prod).
- `ref` entre projetos/pacotes: `ref('pacote', 'modelo')`.
- `{{ this }}` é a relação do próprio modelo (usado em incrementais).

## Materializações

| Tipo | O que cria | Quando |
| --- | --- | --- |
| `view` | view | padrão; leve, staging; custo vai para quem consulta |
| `table` | tabela recriada a cada run | marts médios, lógica que muda |
| `incremental` | tabela; só processa novos/alterados após a 1a carga | tabelas grandes de eventos/fatos |
| `ephemeral` | nada (vira CTE no modelo que o referencia) | reuso de lógica pequena; não dá para consultar nem testar diretamente |
| `materialized_view` | view materializada (se o adapter suportar) | casos específicos |

Snapshots (SCD tipo 2 com `dbt_valid_from`/`dbt_valid_to`) ficam em
`snapshots/`; na 1.9+ podem ser configurados em YAML.

## Incremental

```sql
{{ config(materialized='incremental', unique_key='pedido_id',
          incremental_strategy='merge', on_schema_change='append_new_columns') }}

SELECT * FROM {{ ref('bronze_pedidos') }}
{% if is_incremental() %}
WHERE updated_at > (SELECT MAX(updated_at) FROM {{ this }}) - INTERVAL '2 hours'
{% endif %}
```

- `is_incremental()` é verdadeiro só quando: a relação já existe, é tabela, o
  modelo é `incremental` e **não** foi passado `--full-refresh`. Na 1a execução
  o filtro não se aplica (carga completa).
- `unique_key` (coluna ou lista) define o upsert. **Sem** `unique_key` a
  estratégia vira append → duplica em reprocessamento. A chave tem que ser
  única na fonte do lote; deduplique no próprio modelo.
- Estratégias (dependem do adapter): `append`, `merge`, `delete+insert`,
  `insert_overwrite` (partições), `microbatch` (1.9+, por janelas de tempo
  usando `event_time`, com backfill por `--event-time-start/--event-time-end`).
- `on_schema_change`: `ignore` (padrão — **coluna nova não aparece** na tabela
  existente), `append_new_columns`, `sync_all_columns`, `fail`. Colunas novas
  ficam NULL nas linhas antigas até `--full-refresh` ou backfill.
- `--full-refresh` recria a tabela: em prod é reconstrução completa (custo e
  janela de indisponibilidade). ASK sempre.

## Testes

```yaml
models:
  - name: silver_pedidos
    columns:
      - name: pedido_id
        data_tests: [unique, not_null]
      - name: status
        data_tests:
          - accepted_values:
              arguments:            # 1.10+; em versões anteriores, values direto
                values: ['aberto', 'pago', 'enviado', 'cancelado']
      - name: cliente_id
        data_tests:
          - relationships:
              arguments:
                to: ref('silver_clientes')
                field: cliente_id
```

- `data_tests:` (1.8+) substitui `tests:` (ainda aceito).
- Testes singulares: `.sql` em `tests/` que retorna linhas com problema.
- `config: {severity: warn}` ou `error_if`/`warn_if` para limiares.
- Unit tests (1.8+, `unit_tests:` no YAML) testam a lógica com entradas fixas,
  sem depender de dados do warehouse.
- `dbt source freshness` verifica frescor declarado nas sources.
- Contratos (`config: {contract: {enforced: true}}`) falham o build se nomes e
  tipos de colunas divergirem do YAML.

## Seleção de nós

| Seletor | Significado |
| --- | --- |
| `modelo` | só o modelo |
| `+modelo` | modelo e todos os ancestrais |
| `modelo+` | modelo e todos os descendentes |
| `+modelo+` | ambos |
| `2+modelo` / `modelo+1` | limita a profundidade |
| `@modelo` | descendentes e os ancestrais deles (útil em CI) |
| `tag:diario` | por tag |
| `path:models/gold` | por pasta |
| `config.materialized:incremental` | por config |
| `state:modified+` | alterados vs um manifest de referência (`--state <dir>`) |
| `--exclude ...` | remove da seleção |

União: espaço (`a b`); interseção: vírgula (`tag:x,path:models/gold`).

## Comandos

| Comando | Efeito | Classe |
| --- | --- | --- |
| `dbt deps`, `dbt debug`, `dbt parse` | pacotes, conexão, parse | leitura |
| `dbt ls -s <sel>` | lista nós da seleção | leitura |
| `dbt compile -s <sel>` | gera SQL em `target/compiled/` (consulta o warehouse para introspecção) | leitura |
| `dbt show -s <modelo> --limit 20` | executa o SELECT e mostra linhas | leitura (dados → mascarar) |
| `dbt run -s <sel>` | materializa modelos | **escrita** |
| `dbt test -s <sel>` | roda testes (SELECTs); com `--store-failures` grava tabelas | leitura (escrita se `--store-failures`) |
| `dbt build -s <sel>` | seeds, snapshots, run e test na ordem do DAG; teste falho pula os descendentes | **escrita** |
| `dbt seed`, `dbt snapshot` | carregam/atualizam tabelas | **escrita** |
| `dbt run --empty` | (1.8+) materializa com zero linhas: valida SQL e schema barato | escrita (DDL) |
| `dbt retry` | reexecuta o que falhou na última invocação | conforme o comando |

- `--target dev|prod` escolhe a conexão do `profiles.yml`. **Target prod =
  escrita em prod**: DENY por padrão no DengCode. Sem `--target`, vale o
  `target:` padrão do profile — confira qual é antes de rodar.
- `--vars '{"data_ref": "2025-01-31"}'` para parâmetros; `--defer --state`
  permite usar relações de prod para modelos não selecionados.
- Com DuckDB em arquivo, só um processo escreve por vez: feche outras conexões
  ao arquivo antes do `dbt run`.

## Artefatos em `target/`

- `manifest.json`: todos os nós, `depends_on`, SQL bruto e compilado, configs,
  colunas documentadas, `relation_name`. Fonte do lineage e da impact analysis.
- `run_results.json`: status por nó da última invocação (`success`, `error`,
  `fail`, `warn`, `skipped`, `pass`), tempos e `failures` dos testes.
- `catalog.json` (`dbt docs generate`): colunas e tipos reais no warehouse.
- `target/compiled/` (SELECT compilado) e `target/run/` (DDL/DML executado).

## Fluxo seguro do DengCode para mudar um modelo

1. **Localizar**: `list_models`/manifest → arquivo, relação, materialização,
   testes e dono do modelo.
2. **Impacto**: descendentes até N níveis e lineage de colunas
   (`impact_analysis`); listar modelos, exposições e testes afetados.
3. **Editar**: diff mínimo, aprovado pelo usuário; `ref`/`source` sempre.
   Coluna nova propagada: editar cada modelo do caminho até o destino.
4. **Compilar**: `compile` dos modelos alterados; revisar o SQL compilado.
5. **Rodar em dev**: `run -s <alterados>+` (ou só até o destino) com
   `--target dev`; incremental alterado → considerar `--full-refresh` em dev.
6. **Testar**: `test -s <alterados>+`; comparar com o estado dos testes antes
   da mudança.
7. **Data diff**: versão nova (dev) x atual (prod ou dev anterior): contagem,
   chaves, nulos, colunas alteradas, métricas.
8. **Relatório**: o que mudou, impacto, resultados de run/test, diff e
   pendências (ex.: incremental precisa de backfill em prod). Deploy em prod
   fica com o usuário/CI.

## Armadilhas comuns

- Nome físico hardcoded em vez de `ref`.
- Incremental sem `unique_key`, ou com filtro `>=` sem merge (duplica bordas).
- `on_schema_change` padrão ignorando a coluna nova no incremental.
- `ephemeral` muito reutilizado gerando SQL gigante e lento.
- Testes rodando antes do `run` do modelo alterado (testam a versão antiga).
- Rodar `dbt run` sem `-s` (reconstrói o projeto inteiro).
- Confundir `dbt test` verde com dados corretos: testes só cobrem o que
  declaram; o data diff cobre o resto.
