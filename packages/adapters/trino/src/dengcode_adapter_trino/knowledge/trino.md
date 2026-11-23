# Trino — guia prático para o agente

Trino é um motor SQL distribuído **sem armazenamento próprio**: ele consulta
dados onde eles estão, por meio de **conectores**. Cada conector configurado
vira um **catálogo** (`iceberg`, `hive`, `postgresql`, `memory`...). O nome
completo de uma tabela tem sempre três partes: `catalogo.schema.tabela`.

## Catálogos, schemas e `information_schema`

- `SHOW CATALOGS`, `SHOW SCHEMAS FROM iceberg`, `SHOW TABLES FROM iceberg.vendas`.
- Cada catálogo tem o próprio `information_schema`:
  `SELECT column_name, data_type FROM iceberg.information_schema.columns
  WHERE table_schema = 'vendas' AND table_name = 'pedidos'`.
  Filtre **sempre** por `table_schema` (e `table_name`): sem filtro, o Trino
  lista tudo do catálogo — em um metastore grande isso é lento.
- O catálogo `system` traz metadados do próprio cluster:
  `system.runtime.queries` (consultas em andamento), `system.metadata.catalogs`
  (qual conector cada catálogo usa), `system.metadata.table_comments`.
- Nomes são normalizados para **minúsculas** na maioria dos conectores:
  `"Pedidos"` e `pedidos` costumam ser a mesma tabela.

## Consultas federadas

Um mesmo `SELECT` pode juntar catálogos diferentes:

```sql
SELECT c.nome, sum(p.valor)
FROM iceberg.vendas.pedidos p
JOIN postgresql.public.clientes c ON c.id = p.cliente_id
GROUP BY c.nome
```

Cuidados: o Trino puxa os dados de cada fonte pela rede; filtros e projeções
só são "empurrados" (*pushdown*) para a fonte quando o conector suporta.
Junte primeiro o lado pequeno filtrado; evite `JOIN` entre duas tabelas
grandes de fontes diferentes sem filtro.

## Custo: varreduras completas

- O Trino lê **tudo** o que o plano mandar: `SELECT *` sem `WHERE` em tabela
  de data lake = varredura completa (bytes de S3 = dinheiro e tempo).
- Selecione só as colunas necessárias (formatos colunares como Parquet/ORC
  leem só as colunas pedidas).
- Use `LIMIT` em exploração — mas lembre que `ORDER BY ... LIMIT` ainda precisa
  ler tudo para ordenar.
- `count(DISTINCT x)` em tabela grande é caro (precisa de memória para todos os
  valores). Para estimativas use `approx_distinct(x)` (erro ~2,3%) e
  `approx_percentile(x, 0.5)`.

## EXPLAIN e EXPLAIN ANALYZE

- `EXPLAIN <sql>` mostra o plano distribuído **sem executar** (fragmentos,
  trocas de dados entre nós, estimativas de linhas/bytes quando há
  estatísticas).
- `EXPLAIN (TYPE IO, FORMAT JSON) <sql>` lista tabelas e **partições/
  restrições** que serão lidas — ótimo para confirmar *partition pruning*.
- `EXPLAIN ANALYZE <sql>` **executa** a consulta e mostra o custo real por
  operador (linhas, CPU, memória). Trate como execução: em `INSERT`/`DELETE`
  ele grava de verdade.
- `SHOW STATS FOR iceberg.vendas.pedidos` mostra estatísticas do conector
  (nulos, distintos, min/max) sem varrer a tabela, quando existem.

## Iceberg no Trino: partições e *pruning*

- Tabelas Iceberg usam **particionamento oculto**: a partição é derivada de uma
  coluna por transformação (`day(criado)`, `bucket(16, id)`, `month(dt)`):

  ```sql
  CREATE TABLE iceberg.vendas.pedidos (id bigint, criado timestamp(6) with time zone)
  WITH (partitioning = ARRAY['day(criado)'])
  ```

- Para o *pruning* funcionar, filtre a **coluna original** com comparações
  simples: `WHERE criado >= TIMESTAMP '2026-01-01 00:00:00 UTC'`. Envolver a
  coluna em função (`WHERE date_format(criado, '%Y-%m') = '2026-01'`)
  geralmente impede o *pruning* e vira varredura completa.
- Tabelas de metadados: `"pedidos$snapshots"`, `"pedidos$files"`,
  `"pedidos$partitions"`, `"pedidos$history"`.
- *Time travel*: `SELECT * FROM iceberg.vendas.pedidos FOR VERSION AS OF
  <snapshot_id>` ou `FOR TIMESTAMP AS OF TIMESTAMP '2026-01-01 00:00:00 UTC'`.
- Manutenção: `ALTER TABLE ... EXECUTE optimize` (compacta arquivos pequenos),
  `EXECUTE expire_snapshots(retention_threshold => '7d')`,
  `EXECUTE remove_orphan_files(...)` — todas são **escrita**.

## Datas e horas: onde todo mundo erra

- Literais tipados: `DATE '2026-01-31'`, `TIMESTAMP '2026-01-31 10:00:00'`.
  Comparar `timestamp` com texto (`'2026-01-31'`) pode falhar ou converter de
  forma inesperada; use o literal.
- `timestamp` **não tem fuso**; `timestamp with time zone` tem. O fuso da
  sessão (`SET TIME ZONE 'America/Sao_Paulo'`) muda conversões e `current_date`.
  Prefira guardar em UTC e converter só na apresentação:
  `criado AT TIME ZONE 'America/Sao_Paulo'`.
- `date_trunc('month', ts)`, `date_add('day', 7, d)`, `date_diff('day', a, b)`
  — a unidade vem **primeiro** e entre aspas.
- `date_format(ts, '%Y-%m-%d')` usa formato do MySQL; `format_datetime(ts,
  'yyyy-MM-dd')` usa formato do Java. Não misture os dois.
- `from_unixtime(segundos)` espera **segundos**; epoch em milissegundos precisa
  de `/ 1000` (ou `from_unixtime_nanos`).
- `current_date`, `current_timestamp` e `now()` dependem do fuso da sessão.
- Divisão de inteiros é inteira: `1 / 2 = 0`. Use `CAST(x AS double)` ou
  `x * 1.0 / y`.

## Erros comuns

- `TABLE_NOT_FOUND` / `SCHEMA_NOT_FOUND`: confira o catálogo — sem catálogo na
  sessão, o nome precisa das três partes.
- `EXCEEDED_TIME_LIMIT`: a consulta passou do `query_max_run_time` da sessão.
- `EXCEEDED_LOCAL_MEMORY_LIMIT` / `EXCEEDED_GLOBAL_MEMORY_LIMIT`: o `JOIN` ou
  `GROUP BY` não coube na memória; filtre antes, ponha o lado menor do `JOIN` à
  direita (sem estatísticas o Trino não reordena sozinho) ou agregue em etapas.
- O Trino não tem transações de várias instruções na maioria dos conectores e
  executa **uma instrução por requisição** (sem `;` separando várias).
