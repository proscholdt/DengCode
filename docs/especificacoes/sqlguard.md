# Especificação — SqlGuard, permissões e governança

## Fluxo de toda query

```
ferramenta query_run(sql, engine?, env?)
  → SqlGuard.analyze(sql, dialect)          # parse + classificação
  → PermissionPolicy.decide(analysis, env)  # ALLOW | ASK | DENY
  → CostGuard.check(...)                    # LIMIT, dry-run/explain, teto
  → [aprovação do usuário, se ASK]
  → QueryEnginePort.execute(...)            # com timeout
  → ResultStore.save(parquet) + preview mascarado (PiiMasker)
  → AuditLog.append(...)
```

## Classificação (sqlglot)

```python
class SqlKind(StrEnum):
    READ = "read"  # SELECT, WITH ... SELECT, SHOW, DESCRIBE, EXPLAIN
    DML = "dml"  # INSERT, UPDATE, DELETE, MERGE, COPY INTO
    DDL = "ddl"  # CREATE, ALTER, DROP, TRUNCATE, RENAME
    ADMIN = "admin"  # GRANT, REVOKE, SET, USE, CALL, OPTIMIZE, VACUUM
    UNKNOWN = "unknown"  # parse falhou ou comando não mapeado


class SqlAnalysis(BaseModel):
    kind: SqlKind
    statements: int
    tables_read: list[str]
    tables_written: list[str]
    dangerous: list[str]  # ex.: "DELETE sem WHERE", "DROP TABLE", "TRUNCATE", "UPDATE sem WHERE"
    has_limit: bool
    dialect: str
```

- Várias instruções numa string: a classificação é a de **maior risco**.
- Parse falhou: `UNKNOWN`, tratado como DDL (maior risco).
- Ordem de risco: READ < DML < ADMIN < DDL = UNKNOWN.

## Política padrão

| Kind | dev | prod |
| --- | --- | --- |
| READ | ALLOW | ALLOW |
| DML | ASK | DENY (a menos que `allow_prod_writes: true`) |
| ADMIN | ASK | DENY |
| DDL / UNKNOWN | ASK | DENY |
| `dangerous` não vazio | ASK com aviso destacado | DENY |

Modos de permissão da sessão: `ask` (padrão), `auto` (ALLOW para DML em dev,
nunca em prod), `read_only` (tudo que não é READ vira DENY).

Regras extras configuráveis no `PROJETO.md`/stack, ex.:
`protected_tables: ["gold.fato_vendas"]` → qualquer escrita = DENY.

## Proteção de custo

- READ sem `LIMIT` em modo exploratório → o guard injeta `LIMIT <max_rows>`
  (via sqlglot, preservando o dialeto) e avisa.
- Se `supports_dry_run`: dry-run antes; se `supports_explain`: explain.
- `cost_limit` na stack (bytes, créditos ou US$). Estimativa acima do teto → ASK
  mesmo para READ. Sem estimativa possível e engine marcada `metered: true`
  (Athena, BigQuery…) → ASK.

## Resultados e PII

- Resultado completo (até `max_rows`) → Parquet em
  `~/.dengcode/workspaces/<ws>/cache/results/<id>.parquet`.
- Ao LLM vai: schema, contagem, até 20 linhas e estatísticas simples por coluna.
- `PiiMasker`: mascara colunas com tag PII no catálogo e valores que casam
  detectores (e-mail, CPF, CNPJ, telefone BR/internacional, cartão com Luhn).
  Máscara preserva formato (`j***@d***.com`, `***.***.***-12`).
- Amostras mascaradas vivem só na sessão; nunca vão para a memória persistente.

## Auditoria

`audit.db` (SQLite, append-only):
`id, ts, workspace, stack, env, engine, sql, kind, decision, approved_by,
estimated_cost, actual_cost, rows, elapsed_ms, error`.
Sem valores de linha. Comando `dengcode audit list --since 7d`.

## Arquivos e shell

- Escrita de arquivo só dentro do workspace; sempre gera diff; checkpoint
  (cópia antes da mudança) permite `dengcode undo`.
- Shell: allowlist configurável (`git status`, `uv`, `dbt`, `pytest`...).
  Fora da allowlist → ASK. Comandos destrutivos conhecidos (`rm -rf`,
  `del /s`, `format`) → DENY.

## Injeção de prompt

Conteúdo de dados, logs, comentários de tabela e arquivos entra no contexto
envolto em marcadores de "conteúdo não confiável". Nada que venha dali pode
mudar a política, o modo de permissão ou aprovar ações.
