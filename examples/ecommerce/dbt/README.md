# Projeto dbt de exemplo — e-commerce (DuckDB)

Projeto dbt pequeno, em camadas, sobre os CSVs fictícios de `examples/ecommerce/`
(gerados por `gerar_dados.py`). Serve para exercitar o adapter `dbt_core` do
DengCode e o fluxo "mudar modelo" da fase 4.

| Camada | Modelos | Materialização |
| --- | --- | --- |
| bronze | `bronze_pedidos`, `bronze_clientes`, `bronze_itens_pedido` | view (tudo texto, cópia fiel dos CSVs) |
| silver | `silver_pedidos`, `silver_clientes` | table (deduplicado e tipado) |
| gold | `gold_vendas_diarias` | table (pedidos e receita por dia, sem cancelados) |

A `silver_pedidos` limpa a sujeira da origem: linhas duplicadas, `status` com
caixa misturada, `valor_total` como texto com espaços e `data_pedido` em
`AAAA-MM-DD` e `DD/MM/AAAA`. A coluna `canal` existe na bronze, mas **ainda não**
chega à silver — de propósito (ver o cenário abaixo).

`silver_clientes` mantém as colunas de PII (nome, e-mail, CPF, telefone) como
estão: o mascaramento acontece no DengCode antes de qualquer dado ir ao LLM.

## Como rodar

O banco (`ecommerce.duckdb`) e os caminhos dos CSVs (`../pedidos.csv` etc.) são
relativos ao **diretório deste projeto**, então o dbt precisa rodar com o
diretório atual aqui — é o que o adapter do DengCode faz. Da raiz do repositório:

```
uv run --directory examples/ecommerce/dbt dbt build --project-dir . --profiles-dir .
```

ou, já dentro desta pasta: `uv run dbt build --profiles-dir .`

O `build` cria as views/tabelas e roda os testes (`unique`/`not_null` em
`silver_pedidos.id_pedido`, `accepted_values` em `silver_pedidos.status`,
`not_null`/`unique` em `gold_vendas_diarias.data_pedido`, chave de
`silver_clientes`). Tudo deve sair verde.

Arquivos gerados (`target/`, `logs/`, `*.duckdb`, `.user.yml`) não vão para o git.

## Cenário da fase 4

Com a stack apontando o adapter `dbt_core` para esta pasta, peça ao DengCode:

> adicione a coluna `canal_venda` em `silver_pedidos` (a partir de `canal` da
> bronze) e propague até `gold_vendas_diarias`, com as vendas por dia e canal

O esperado: lista de impacto (`silver_pedidos` → `gold_vendas_diarias`),
edições aprovadas, `dbt run`/`dbt test` verdes e um relatório de data diff
mostrando o que mudou.
