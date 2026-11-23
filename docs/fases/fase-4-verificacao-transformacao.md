# Fase 4 — Verificação e transformação

**Objetivo:** o agente altera pipelines com confiança: entende dependências,
roda testes e prova com dados que a mudança está correta.

## Tarefas

1. **TransformPort** + adapter `dbt-core` (programático via `dbtRunner`;
   fallback para subprocesso): `list_models`, `compile`, `run`, `test`,
   `manifest_lineage` (lendo `manifest.json`).
2. **Lineage de colunas** (`dengcode_core/lineage/`) com `sqlglot.lineage`:
   a partir do SQL compilado dos modelos, gera arestas tabela.coluna → tabela.coluna.
   Une com o lineage do catálogo quando existir.
3. **Ferramenta `impact_analysis`**: dado modelo/coluna, lista o que está
   downstream (até N níveis) com o caminho.
4. **Verifier / data diff** (`dengcode_core/verify/`):
   - compara duas relações (antes/depois, ou dev vs prod) via SQL gerado pelo
     sqlglot no dialeto do engine: contagem de linhas, chaves duplicadas,
     nulos por coluna, linhas só de um lado (por chave), min/max/média de
     numéricas, top valores de categóricas;
   - saída estruturada (`DataDiffReport`) + resumo legível;
   - padrão: rodar a versão nova num schema temporário de dev (`dengcode_tmp_<sessão>`)
     e comparar com a atual.
5. **Fluxo "mudar modelo"** no prompt/planner: localizar modelo → impacto →
   editar → `compile` → `run` em dev → `test` → data diff → relatório.
6. **Fundamentos** em `knowledge/fundamentos/` (markdown curto e objetivo, ~1–2
   páginas cada): modelagem dimensional, SCD, medallion, incremental e
   idempotência, backfill, CDC, qualidade de dados, particionamento, LGPD.
   Mais `knowledge/index.yaml` (título + resumo de 1 linha por arquivo).
7. **Ferramenta `knowledge_read`** + índice no prompt; pacote de conhecimento
   dos adapters existentes (`duckdb`, `postgres`, `dbt`).
8. **Projeto dbt de exemplo** em `examples/ecommerce/dbt/` (bronze→silver→gold, DuckDB).

## Critério de pronto

- [ ] Pedido "adicione a coluna `canal_venda` em `silver_pedidos` e propague até
      `gold_vendas_diarias`" resulta em: lista de impacto, edições aprovadas,
      `dbt run`/`test` verdes e relatório de data diff mostrando o que mudou.
- [ ] Lineage de colunas correto em testes com SQL de vários dialetos.
- [ ] Data diff testado com casos de: linhas a mais, duplicadas, nulos novos.
- [ ] CI verde.
