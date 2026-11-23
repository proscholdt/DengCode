# DuckDB — pacote de conhecimento

Engine analítica embutida (processo local), colunar, dialeto próximo ao
PostgreSQL com extensões próprias. Lê CSV/Parquet/JSON direto de arquivos e
globs. Tudo abaixo foi conferido no DuckDB 2.0.

## Metadados e inspeção (leitura, sempre permitido)

```sql
SHOW TABLES;                      -- tabelas do schema atual
SHOW ALL TABLES;                  -- todas, com colunas e tipos
DESCRIBE vendas;                  -- colunas e tipos de uma tabela
DESCRIBE SELECT * FROM read_csv('dados/*.csv');   -- tipos inferidos de um arquivo
SUMMARIZE vendas;                 -- min, max, distintos aprox., média, quartis, % nulos
SUMMARIZE SELECT valor FROM vendas;
SELECT * FROM information_schema.columns WHERE table_name = 'vendas';
SELECT * FROM duckdb_tables();    -- inclui estimated_size, column_count
SELECT * FROM duckdb_columns();
PRAGMA table_info('vendas');
SELECT * FROM sniff_csv('arquivo.csv');        -- delimitador, header, tipos detectados
SELECT * FROM parquet_schema('arquivo.parquet');
SELECT * FROM parquet_metadata('arquivo.parquet');  -- row groups, min/max, compressão
SELECT * FROM glob('dados/**/*.parquet');      -- lista arquivos que o glob pega
```

- `SUMMARIZE` **lê a tabela inteira**. Em tabela/arquivo grande, resuma uma
  amostra: `SUMMARIZE SELECT * FROM t USING SAMPLE 100000 ROWS`.
- Identificadores são *case-insensitive* (a grafia original é preservada):
  `"MinhaTabela"` e `minhatabela` são a mesma tabela.

## Leitura de arquivos

```sql
SELECT * FROM 'dados/vendas.parquet';            -- atalho por extensão
SELECT * FROM read_parquet('dados/vendas/**/*.parquet');
SELECT * FROM read_csv('dados/*.csv', union_by_name = true, filename = true);
SELECT * FROM read_csv(['a.csv', 'b.csv']);      -- lista explícita
SELECT * FROM read_json('eventos.ndjson', format = 'newline_delimited');
```

Opções de `read_csv` que mais importam:

| Opção | Uso |
| --- | --- |
| `delim` (ou `sep`) | `';'` em CSV brasileiro |
| `header` | `true/false`; o sniffer erra em arquivos só com texto |
| `all_varchar = true` | lê tudo como texto; ideal para bronze; tipagem depois com `TRY_CAST` |
| `types = {'col': 'INTEGER'}` / `columns = {...}` | força tipos de colunas / define todas as colunas |
| `dateformat = '%d/%m/%Y'`, `timestampformat` | datas fora do ISO |
| `decimal_separator = ','` | números `10,50` |
| `nullstr = ['NA', '']` | strings que viram NULL |
| `skip = 2` | pular linhas antes do cabeçalho |
| `sample_size = -1` | usar o arquivo inteiro para inferir tipos (mais lento, evita erro na linha 1 milhão) |
| `ignore_errors = true` | **descarta** linhas inválidas silenciosamente — evite em pipeline |
| `store_rejects = true` | guarda as rejeitadas nas tabelas temporárias `reject_errors`/`reject_scans` — prefira a `ignore_errors` |
| `union_by_name = true` | arquivos com colunas diferentes: casa por nome, falta vira NULL |
| `filename = true` | adiciona coluna com o arquivo de origem (linhagem no bronze) |
| `normalize_names = true` | nomes de coluna em snake_case sem caracteres especiais |
| `encoding = 'latin-1'` | arquivos que não são UTF-8 |

- Glob: `*` (um nível), `**` (recursivo). Sem `union_by_name`, arquivos com
  número de colunas diferente falham (`Schema mismatch between globbed files`)
  e, com o mesmo número, as colunas são casadas por **posição** — renomear ou
  reordenar colunas num arquivo troca valores silenciosamente.
- `read_parquet` detecta partições Hive (`ano=2024/`) e cria a coluna;
  `hive_partitioning = false` desliga. Filtro na coluna de partição faz pruning.
- `read_json`: `format = 'auto' | 'newline_delimited' | 'array'`;
  `read_json_auto` e `read_csv_auto` são equivalentes às versões com detecção.

## Funções que costumam sair erradas

### Conversão

- `CAST(x AS INTEGER)` **falha** com valor inválido; `TRY_CAST(x AS INTEGER)`
  devolve NULL. Em bronze → silver, use `TRY_CAST` e **conte os NULLs gerados**
  (NULL novo = dado inválido escondido).
- `'1,5'::DOUBLE` falha. Número BR:
  `TRY_CAST(REPLACE(REPLACE(s, '.', ''), ',', '.') AS DECIMAL(18,2))`.
- `CAST(x AS DECIMAL(p,s))` **arredonda** (`'123.456'` → `123.46`) e falha se
  exceder a precisão (`'1234.5'` em `DECIMAL(5,2)`). Dinheiro: `DECIMAL(18,2)`,
  nunca `DOUBLE` (`0.1::DOUBLE + 0.2::DOUBLE = 0.3` é falso).
- Literal `0.1` é `DECIMAL`, não `DOUBLE`.
- `7 / 2 = 3.5` (divisão real); divisão inteira é `7 // 2`. Diferente do
  PostgreSQL.
- `ROUND(2.5) = 3` (metade se afasta do zero).
- `SUM` de inteiros retorna `HUGEINT`; `AVG` retorna `DOUBLE`.

### Datas

- **Não existem** `to_date`, `try_to_date` nem `to_timestamp(texto, formato)`.
  `to_timestamp(n)` só converte epoch em segundos (retorna `TIMESTAMPTZ`).
- Texto → data com formato: `strptime(s, '%d/%m/%Y')` retorna **TIMESTAMP**;
  converta: `strptime(s, '%d/%m/%Y')::DATE`. Falha se não casar.
- `try_strptime(s, fmt)` devolve NULL se não casar. Aceita lista de formatos.
- Coluna com formatos misturados:
  ```sql
  COALESCE(try_strptime(s, '%Y-%m-%d'), try_strptime(s, '%d/%m/%Y'))::DATE
  -- equivalente: try_strptime(s, ['%Y-%m-%d', '%d/%m/%Y'])::DATE
  ```
  Ordem importa quando um valor casa com os dois formatos; `%d/%m` e `%m/%d`
  são ambíguos até o dia 12 — confirme pela distribuição, não pela 1a linha.
- `CAST('05/03/2024' AS DATE)` falha (só ISO `YYYY-MM-DD`);
  `TRY_CAST` devolve NULL.
- Data inexistente (`31/02/2024`) → `try_strptime` devolve NULL.
- Data → texto: `strftime(data, '%d/%m/%Y')`, `strftime(ts, '%Y-%m')`.
- `date_trunc('month', x)` retorna **TIMESTAMP** mesmo com entrada DATE:
  use `date_trunc('month', x)::DATE` ao agrupar/juntar com colunas DATE.
  Partes: `'day'`, `'week'` (segunda-feira), `'month'`, `'quarter'`, `'year'`.
- `date_diff('month', DATE '2024-01-31', DATE '2024-02-01') = 1`: conta
  **fronteiras**, não meses completos. `data1 - data2` entre DATEs dá dias
  (BIGINT).
- Fuso: `TIMESTAMPTZ` é exibido no fuso da sessão (`current_setting('TimeZone')`,
  vem do sistema). Para resultados reprodutíveis: `SET TimeZone = 'UTC'`.
  `'...Z'` → `CAST(s AS TIMESTAMPTZ)`; `%z` em `strptime` aceita `+00:00`.

### Deduplicação e agregação

```sql
-- Última versão por chave (determinística com desempate)
SELECT * FROM bronze_pedidos
QUALIFY ROW_NUMBER() OVER (PARTITION BY pedido_id
                           ORDER BY updated_at DESC, _arquivo DESC) = 1;

-- Equivalente
SELECT DISTINCT ON (pedido_id) * FROM bronze_pedidos
ORDER BY pedido_id, updated_at DESC;
```

- `SELECT DISTINCT` só remove linhas idênticas em **todas** as colunas; não
  deduplica por chave.
- `arg_max(valor, data)` / `max_by`: valor da linha com maior `data`.
- `count(*) FILTER (WHERE ...)`, `string_agg(x, ',' ORDER BY x)`,
  `approx_count_distinct(x)` (rápido, aproximado).
- `ORDER BY` coloca NULL **por último tanto em ASC quanto em DESC** (no
  PostgreSQL, DESC põe NULL primeiro). Explicite `NULLS FIRST/LAST`.
- `'a' || NULL` é NULL; `concat('a', NULL)` = `'a'`; `concat_ws` ignora NULL.
  Para hash de linha: `md5(concat_ws('|', COALESCE(a,''), ...))` ou `hash(a, b)`.
  `hash(*)` não existe.

### Açúcar sintático útil

`SELECT * EXCLUDE (col)`, `SELECT * REPLACE (upper(nome) AS nome)`,
`COLUMNS('regex')`, `GROUP BY ALL`, `ORDER BY ALL`, `FROM t SELECT ...`,
`UNION ALL BY NAME`, `ANTI JOIN`/`SEMI JOIN`, `USING SAMPLE 10 PERCENT (bernoulli)`.

## Escrita (classificada como DDL/DML pelo SqlGuard)

- `CREATE OR REPLACE TABLE t AS SELECT ...` **descarta a tabela anterior** sem
  aviso. Para versão nova, crie em schema temporário
  (`CREATE SCHEMA IF NOT EXISTS dengcode_tmp_<sessao>`) e compare.
- Upsert: `INSERT INTO t ... ON CONFLICT (id) DO UPDATE SET v = excluded.v`
  (exige PK/UNIQUE), `INSERT OR REPLACE INTO t ...`, ou `MERGE INTO`.
- `MERGE` com chave **duplicada na fonte não dá erro**: atualiza a mesma linha
  várias vezes e o resultado depende da ordem. Deduplique a fonte antes.
- `COPY (SELECT ...) TO 'saida.parquet' (FORMAT parquet)` **escreve arquivos**
  no disco — não é leitura. Com `PARTITION_BY (col)`, falha se o diretório não
  estiver vazio; `OVERWRITE` **apaga o conteúdo do diretório**;
  `APPEND` adiciona arquivos. `COPY t FROM 'arquivo'` insere linhas (DML).
- `EXPLAIN` mostra o plano sem executar. `EXPLAIN ANALYZE` **executa** a
  instrução (um `EXPLAIN ANALYZE INSERT` insere de verdade).

## Concorrência e arquivo `.duckdb`

- **Um único processo escritor por arquivo.** Enquanto um processo mantém o
  arquivo aberto para escrita, outro processo não consegue abri-lo — nem em
  modo leitura (`read_only=True` também falha). Vários processos só leitura
  simultâneos funcionam, mas enquanto houver um leitor aberto nenhum outro
  processo consegue abrir para escrita.
- Dentro do mesmo processo, várias conexões/cursores sobre a mesma instância
  funcionam.
- Por isso o adapter do DengCode abre uma conexão **por operação** em bancos de
  arquivo: entre consultas, outro processo (ex.: a ferramenta de transformação)
  pode escrever. Se aparecer `IO Error: Cannot open file ... ` / arquivo em uso,
  há outro processo com o arquivo aberto; não tente apagar o arquivo de lock.
- `ATTACH 'outro.duckdb' AS o (READ_ONLY)` para ler outro banco sem risco.

## Performance e custo

- Custo é tempo/memória local. `SET memory_limit = '4GB'`, `SET threads = 4`
  quando dividir a máquina; acima do limite a maioria dos operadores derrama para disco (temp_directory).
- Parquet: selecione só as colunas necessárias e filtre por colunas de partição
  ou ordenadas (pruning por row group). CSV sempre é lido inteiro.
- Muitos arquivos pequenos degradam leitura; consolide com `COPY`.
- `preserve_insertion_order = false` reduz memória em cargas grandes quando a
  ordem não importa.

## Segurança

- `CREATE SECRET` (temporário por padrão) guarda credenciais de S3/HTTP na
  sessão; **não** coloque chaves em SQL versionado. `CREATE PERSISTENT SECRET`
  grava a credencial em disco sem criptografia — evite.
- Arquivos lidos por `read_*` podem conter PII: o preview passa pelo
  mascaramento, mas `COPY ... TO` exporta dados crus para fora do controle.

## Como o agente deve agir

1. Arquivo novo: `DESCRIBE SELECT * FROM read_csv(...)` e `sniff_csv` antes de
   carregar; confira delimitador, header e tipos inferidos.
2. Bronze: `all_varchar = true` + `filename = true`; silver: `TRY_CAST`/
   `try_strptime` explícitos e contagem de NULLs gerados por coluna.
3. Nunca use `ignore_errors` sem relatar quantas linhas foram descartadas
   (use `store_rejects` e consulte `reject_errors`).
4. Versão nova de tabela em `dengcode_tmp_<sessao>` e data diff contra a atual.
5. Erro de arquivo em uso: espere o outro processo terminar; não force.
