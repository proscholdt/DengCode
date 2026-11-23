# PostgreSQL — pacote de conhecimento

Banco relacional transacional (OLTP), muito usado também como warehouse
pequeno/médio e como origem de CDC. Linha a linha, com índices B-tree, MVCC e
locks. Em geral é **origem operacional**: toda consulta pesada concorre com a
aplicação. Válido para PostgreSQL 14+ (notas de versões mais novas indicadas).

## Metadados: `information_schema` x `pg_catalog`

- `information_schema` é padrão SQL, portátil, mais lento e **só mostra objetos
  sobre os quais o usuário tem privilégio**. Bom para colunas e tipos.
- `pg_catalog` é nativo, completo (tamanhos, estatísticas, índices, partições).

```sql
-- Colunas
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'pedidos'
ORDER BY ordinal_position;

-- Tamanho e linhas estimadas (sem varrer a tabela)
SELECT c.relname, c.reltuples::bigint AS linhas_estimadas,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS tamanho_total
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY pg_total_relation_size(c.oid) DESC;

-- Índices de uma tabela
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'pedidos';

-- Constraints (PK, FK, UNIQUE)
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conrelid = 'public.pedidos'::regclass;

-- Manutenção e atividade da tabela
SELECT relname, n_live_tup, n_dead_tup, last_analyze, last_autovacuum
FROM pg_stat_user_tables WHERE relname = 'pedidos';

-- Comentários
SELECT obj_description('public.pedidos'::regclass);
```

- `reltuples = -1` significa tabela nunca analisada.
- `pg_stats` traz `null_frac`, `n_distinct`, mas também `most_common_vals`
  com **valores reais** das colunas — pode expor PII. Use só `null_frac` e
  `n_distinct` para perfil.
- `SELECT COUNT(*)` em tabela grande faz varredura completa; prefira
  `reltuples` para ordem de grandeza.

## Identificadores e tipos

- Nomes sem aspas são convertidos para **minúsculas**: `SELECT Nome FROM Pedidos`
  procura `nome` em `pedidos`. Tabela criada como `"Pedidos"` só é acessível com
  aspas, sempre. Gere SQL com aspas duplas quando o nome tiver maiúscula,
  espaço ou acento.
- Strings usam aspas simples; aspas duplas são identificadores.
- `7 / 2 = 3` (divisão inteira entre inteiros). Use `7::numeric / 2` ou `7 / 2.0`.
- Dinheiro: `numeric(18,2)`, não `real`/`double precision`. Evite o tipo `money`.
- `timestamp` (sem fuso) x `timestamptz` (instante absoluto, exibido no
  `TimeZone` da sessão). Prefira `timestamptz` para eventos.
- `now()`/`current_timestamp` = início da **transação**; `clock_timestamp()` =
  agora de fato.
- `ORDER BY x DESC` põe NULL **primeiro** (ASC põe por último). Explicite
  `NULLS LAST`.

## Datas: `to_date` / `to_timestamp`

```sql
SELECT to_date('05/03/2024', 'DD/MM/YYYY');                 -- date
SELECT to_timestamp('05/03/2024 14:30', 'DD/MM/YYYY HH24:MI');  -- timestamptz
SELECT to_timestamp(1700000000);                            -- epoch → timestamptz
SELECT to_char(now(), 'YYYY-MM-DD');                         -- data → texto
SELECT date_trunc('month', ts);         -- timestamp(tz); com date na entrada, use ::date
SELECT '2024-03-05'::date;                                   -- ISO direto
```

- Formatos usam `YYYY`, `MM`, `DD`, `HH24`, `MI`, `SS` (não `%Y`). `HH` é 12h.
- `MM` é mês e `MI` é minuto — erro clássico.
- `to_date` é tolerante com separadores e espaços; valide o resultado em
  amostra antes de confiar.
- Não existe `TRY_CAST`. Opções: filtrar com regex antes do cast
  (`WHERE s ~ '^\d{2}/\d{2}/\d{4}$'`) ou, no PostgreSQL 16+,
  `pg_input_is_valid(s, 'date')` / `pg_input_is_valid(s, 'numeric')`.
- Formatos misturados:
  ```sql
  CASE WHEN s ~ '^\d{4}-\d{2}-\d{2}$' THEN s::date
       WHEN s ~ '^\d{2}/\d{2}/\d{4}$' THEN to_date(s, 'DD/MM/YYYY') END
  ```

## Deduplicação: `DISTINCT ON`

```sql
SELECT DISTINCT ON (pedido_id) *
FROM pedidos_eventos
ORDER BY pedido_id, updated_at DESC, evento_id DESC;
```

O `ORDER BY` deve começar pelas mesmas expressões do `DISTINCT ON`; o resto
define o vencedor. Sem desempate, o resultado é não determinístico. Alternativa
portátil: `ROW_NUMBER()` numa subconsulta (não há `QUALIFY`).

## Upsert: `ON CONFLICT` e `MERGE`

```sql
INSERT INTO silver_clientes (cliente_id, nome, updated_at)
SELECT cliente_id, nome, updated_at FROM stg_clientes_dedup
ON CONFLICT (cliente_id) DO UPDATE
SET nome = EXCLUDED.nome, updated_at = EXCLUDED.updated_at
WHERE silver_clientes.updated_at < EXCLUDED.updated_at;
```

- Exige **índice/constraint único** exatamente nas colunas do `ON CONFLICT`.
- Fonte com chave repetida → erro `ON CONFLICT DO UPDATE command cannot affect
  row a second time`. Deduplique antes.
- `ON CONFLICT DO NOTHING` ignora silenciosamente; conte o que foi ignorado.
- `MERGE` existe desde a versão 15 (com `RETURNING` a partir da 17); fonte com
  chave duplicada também gera erro.

## `EXPLAIN` x `EXPLAIN ANALYZE`

- `EXPLAIN` mostra o plano estimado **sem executar** — seguro.
- `EXPLAIN ANALYZE` **executa** a consulta. Num `UPDATE/DELETE/INSERT`,
  altera dados. Para medir DML sem efeito:
  `BEGIN; EXPLAIN ANALYZE UPDATE ...; ROLLBACK;` (ainda assim trava linhas e
  consome recursos — não faça em prod).
- `EXPLAIN (ANALYZE, BUFFERS)` mostra I/O (no 18, `BUFFERS` já vem por padrão
  com `ANALYZE`).
- Sinais no plano: `Seq Scan` em tabela grande com filtro seletivo (falta
  índice), estimativa de linhas muito diferente do real (estatísticas velhas →
  `ANALYZE`), `Nested Loop` com milhões de linhas, `Sort` com `external merge`
  (pouca `work_mem`).

## Timeouts, locks e transações longas

```sql
SET statement_timeout = '60s';        -- aborta consulta lenta (por sessão)
SET lock_timeout = '5s';              -- não espera lock indefinidamente
SET idle_in_transaction_session_timeout = '5min';
-- 17+: transaction_timeout
```

- Toda consulta do agente deve ter `statement_timeout` (o adapter aplica o
  timeout da ferramenta).
- MVCC: leitura não bloqueia escrita. Mas **transação longa aberta** (inclusive
  só de leitura) impede o vacuum de limpar versões antigas → inchaço. Não deixe
  transações abertas.
- DDL (`ALTER TABLE`, `DROP`, `TRUNCATE`) pega lock `ACCESS EXCLUSIVE`: espera
  qualquer leitura em andamento e, enquanto espera, **bloqueia todas as
  consultas novas** na tabela. Sempre com `lock_timeout` baixo.
- Diagnóstico:
  ```sql
  SELECT pid, state, now() - xact_start AS idade_tx, wait_event_type, left(query, 80)
  FROM pg_stat_activity WHERE datname = current_database() ORDER BY xact_start;
  SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity
  WHERE cardinality(pg_blocking_pids(pid)) > 0;
  ```
  `pg_cancel_backend`/`pg_terminate_backend` são ações administrativas: só com
  aprovação explícita.

## Índices para joins e filtros

- B-tree em colunas de filtro seletivo, de join (FKs **não** são indexadas
  automaticamente) e de ordenação.
- Índice composto: igualdade primeiro, intervalo por último
  (`(cliente_id, data_pedido)` serve `WHERE cliente_id = ? AND data_pedido >= ?`).
- Função na coluna desliga o índice (`WHERE date(ts) = ...`); use intervalo
  (`ts >= '2024-03-05' AND ts < '2024-03-06'`) ou índice de expressão.
- `CREATE INDEX CONCURRENTLY` evita bloquear escritas (não roda dentro de
  transação; pode deixar índice inválido se falhar). Criar índice é DDL: ASK.
- Tabelas grandes de eventos: particionamento declarativo por data
  (`PARTITION BY RANGE`) + índice BRIN em coluna correlacionada com a ordem
  física.

## `VACUUM` / `ANALYZE` (operações administrativas)

- `ANALYZE t` atualiza estatísticas do planejador; recomendado após carga em
  massa. `VACUUM` recupera espaço de linhas mortas; `VACUUM FULL` reescreve a
  tabela com lock exclusivo.
- Normalmente o autovacuum cuida disso. Para o DengCode são `ADMIN`: ASK em dev,
  DENY em prod. `VACUUM` não roda dentro de transação.

## Carga e exportação

- `COPY ... FROM/TO 'arquivo'` lê/escreve no **servidor** (exige privilégio);
  o equivalente do lado do cliente é `\copy` (psql) ou a API de COPY do driver.
- Carga em massa: `COPY` para tabela de staging, depois `INSERT ... SELECT`/merge
  na tabela final, depois `ANALYZE`.

## Como o agente deve agir

1. Trate o banco como possível **produção operacional**: consultas com filtro
   e `LIMIT`, sem `COUNT(*)`/varredura completa em tabelas grandes sem motivo;
   use `reltuples` e `EXPLAIN` primeiro.
2. Inspecione PK, UNIQUE e índices antes de propor upsert ou join.
3. Nunca rode `EXPLAIN ANALYZE` em DML fora de dev e sem `ROLLBACK`.
4. Aspas duplas em identificadores com maiúscula; nunca concatene valores em
   SQL — use parâmetros.
5. Escrita, DDL, `VACUUM`, cancelamento de sessão: passam pela política
   (ASK/DENY) com `lock_timeout` definido.
