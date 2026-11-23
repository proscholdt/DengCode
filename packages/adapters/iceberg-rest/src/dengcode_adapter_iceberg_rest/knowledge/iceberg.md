# Apache Iceberg — guia prático para o agente

Iceberg é um **formato de tabela** aberto para data lakes: os dados ficam em
arquivos (Parquet, ORC, Avro) num storage de objetos, e uma árvore de
**metadados** diz quais arquivos formam cada versão da tabela. Qualquer motor
que entenda Iceberg (Trino, Spark, Flink, DuckDB...) lê a mesma tabela.

## Camadas de metadados

```
catálogo  →  metadata.json (schema, partições, snapshots, propriedades)
          →  manifest list (um por snapshot)
          →  manifests (listas de arquivos + estatísticas por coluna)
          →  arquivos de dados / arquivos de deleção
```

O **catálogo** guarda só o ponteiro para o `metadata.json` atual. Um commit é
a troca atômica desse ponteiro — por isso duas escritas simultâneas não se
corrompem: a segunda detecta o conflito e refaz.

## Catálogo REST

A spec **Iceberg REST Catalog** padroniza esse ponteiro como API HTTP:

- `GET /v1/config` — configuração do cliente (ex.: `prefix` do warehouse);
- `GET /v1/{prefix}/namespaces` — namespaces (podem ser aninhados:
  `vendas.bronze`; na URL os níveis são separados por `%1F`);
- `GET /v1/{prefix}/namespaces/{ns}/tables` — tabelas do namespace;
- `GET /v1/{prefix}/namespaces/{ns}/tables/{tabela}` — metadados completos.

Autenticação comum: token *bearer* fixo ou OAuth2 *client credentials*.
Implementações: Apache Polaris, Lakekeeper, Nessie, Unity Catalog, Gravitino,
AWS Glue/S3 Tables (endpoint REST) e o servidor de referência
`iceberg-rest-fixture`. Um "namespace" equivale ao "schema" dos motores SQL.

## Snapshots e *time travel*

- Cada commit cria um **snapshot** imutável com resumo (`summary`):
  `operation` (append, overwrite, delete, replace), `added-records`,
  `total-records`, `total-data-files`...
- *Time travel* lê um snapshot antigo: no Trino,
  `FOR VERSION AS OF <snapshot_id>` ou `FOR TIMESTAMP AS OF <ts>`; no Spark,
  `VERSION AS OF` / `TIMESTAMP AS OF`.
- Use para **comparar antes/depois** de uma mudança de pipeline (diff entre
  dois snapshots) e para recuperar dados apagados por engano.
- **Branches e tags** (`refs`): `main` é o branch padrão; uma tag fixa um
  snapshot (ex.: fechamento do mês) e o protege da expiração.

## Evolução de schema

- Colunas têm **ID** interno; renomear, reordenar, adicionar e remover colunas
  é só metadado — **não reescreve dados**.
- Promoções de tipo seguras: `int → long`, `float → double`,
  `decimal(P,S) → decimal(P2,S)` com `P2 > P`. Mudanças incompatíveis
  (ex.: `string → int`) exigem coluna nova + migração.
- Colunas obrigatórias (`required`) não podem ser adicionadas a uma tabela com
  dados sem valor padrão; prefira colunas opcionais.
- O agente deve tratar mudança de schema como mudança de contrato: confira
  consumidores (lineage) antes de remover ou renomear.

## Particionamento oculto e evolução de partição

- A partição é uma **transformação** de uma coluna: `year`, `month`, `day`,
  `hour`, `bucket(N, col)`, `truncate(L, col)`, `identity`.
- O usuário filtra a coluna original (`WHERE criado >= ...`) e o motor deriva
  as partições — não existe coluna `dt` "falsa" para lembrar de filtrar.
- **Evolução de partição**: trocar `month(criado)` por `day(criado)` vale só
  para os dados novos; os antigos continuam no layout antigo e as consultas
  funcionam nos dois. Não é preciso reescrever a tabela.
- Partição demais (ex.: `hour` em tabela pequena) gera milhares de arquivos
  minúsculos — pior que não particionar.

## Manutenção (operações de escrita!)

- **Compactação** (`rewrite_data_files` / Trino `EXECUTE optimize`): junta
  arquivos pequenos; essencial em tabelas com *streaming* ou muitos `INSERT`s.
- **Expirar snapshots** (`expire_snapshots`): apaga snapshots antigos e os
  arquivos que só eles usavam. Depois disso o *time travel* para essas versões
  deixa de funcionar — combine a retenção com o time.
- **Arquivos órfãos** (`remove_orphan_files`): remove arquivos que nenhum
  snapshot referencia (ex.: escritas que falharam). Use retenção de pelo menos
  alguns dias para não apagar arquivos de um commit em andamento.
- **Reescrever manifests** (`rewrite_manifests`): acelera o planejamento de
  consultas em tabelas com muitos commits.

## Deleções: *copy-on-write* × *merge-on-read*

- **Copy-on-write**: `DELETE`/`UPDATE` reescrevem os arquivos afetados —
  leitura rápida, escrita cara.
- **Merge-on-read** (format v2+): grava arquivos de deleção (posicionais ou por
  igualdade) e o leitor aplica na hora — escrita barata, leitura degrada até a
  próxima compactação.

## Dicas para investigar

- Tabela "lenta"? Olhe quantidade e tamanho dos arquivos (`$files`) e o número
  de snapshots (`$snapshots`).
- Contagem diferente do esperado? Compare `total-records` entre snapshots e
  veja a `operation` de cada commit (`$history`, `$snapshots`).
- Dados "sumiram"? Verifique se houve `overwrite`/`delete` recente e se o
  snapshot anterior ainda existe para *time travel*.
