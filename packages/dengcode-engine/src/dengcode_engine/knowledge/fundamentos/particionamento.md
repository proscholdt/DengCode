# Particionamento e organização física

## Quando usar / por que importa

Use ao criar tabelas grandes, ao investigar consultas lentas/caras ou ao
planejar cargas incrementais e backfills. Em engines que cobram ou demoram por
volume lido, a organização física decide se uma consulta lê 1 GB ou 1 TB.
Particionamento também define a unidade natural de sobrescrita (trocar a
partição de um dia inteiro é atômico e idempotente). Particionar errado —
por coluna de alta cardinalidade, por exemplo — piora tudo.

## Conceitos

- **Partição**: divisão física da tabela por valor de uma ou mais colunas
  (diretórios/segmentos separados). A engine **pula partições inteiras**
  quando o filtro permite (*partition pruning*).
- **Clustering / ordenação / zonas**: dentro da tabela (ou de cada partição),
  os dados são ordenados ou agrupados por colunas, e cada arquivo/bloco guarda
  min/max por coluna. A engine pula blocos cujo intervalo não casa com o filtro
  (*data skipping*). Funciona para colunas de alta cardinalidade, onde partição
  não funciona.
- **Índice** (bancos relacionais): estrutura auxiliar para buscas seletivas;
  diferente de partição. Em colunar analítico raramente é a ferramenta certa.
- **Formatos colunares** guardam estatísticas por bloco; ler só as colunas
  necessárias já reduz I/O (evite `SELECT *`).

| | Partição | Clustering/ordenação |
| --- | --- | --- |
| Granularidade | grossa (dia, mês, região) | fina (blocos) |
| Cardinalidade ideal | baixa/média (dezenas a poucos milhares de valores) | alta (id de cliente, timestamp) |
| Pruning | exato, por metadado | probabilístico, por min/max |
| Custo de errar | small files, metadados explodem | pouco efeito, manutenção |
| Unidade de sobrescrita | sim | não |

## Regras de decisão

- Particione pela coluna mais usada em **filtros de intervalo** e na **carga**
  — quase sempre a data (do evento ou da ingestão).
- Data do evento x data de ingestão: evento é melhor para consulta; ingestão é
  melhor para carga append e reprocessamento (dados atrasados não reabrem
  partições antigas). Escolha pela consulta dominante e documente.
- Granularidade: cada partição deve ter volume relevante (ordem de centenas de
  MB a alguns GB por partição em formatos colunares). Pouco dado por dia →
  particione por mês, ou não particione.
- Tabela pequena (abaixo de alguns GB): normalmente **não particione**;
  ordenação/clustering basta.
- Nunca particione por coluna de alta cardinalidade (id de cliente, timestamp
  com segundos). Use clustering/ordenação para elas.
- No máximo 1–2 níveis de partição. Combinações (data x país x produto)
  multiplicam diretórios.
- Ordene/clusterize pelas colunas de filtro e join mais frequentes depois da
  partição.

## Pruning: o filtro tem que permitir

O pruning só acontece se o predicado for aplicável direto à coluna de partição:

```sql
-- Faz pruning
WHERE data_evento >= DATE '2025-06-01' AND data_evento < DATE '2025-07-01'

-- Pode NÃO fazer pruning (função sobre a coluna)
WHERE CAST(data_evento AS VARCHAR) LIKE '2025-06%'
WHERE EXTRACT(MONTH FROM data_evento) = 6

-- Não faz pruning na tabela grande se o valor só vem de um join
-- (depende de otimizações dinâmicas da engine)
WHERE data_evento = (SELECT MAX(data) FROM controle)
```

- Tipos diferentes (comparar coluna DATE com texto, ou TIMESTAMP com DATE)
  podem desligar o pruning por causa de cast implícito.
- Partição derivada (ex.: `ano`, `mes` como colunas separadas): o filtro precisa
  usar essas colunas, não a data original — a menos que a engine suporte
  partição oculta/derivada.

## Small files e compactação

- Muitos arquivos pequenos (KB a poucos MB) custam mais em abertura e
  metadados do que em leitura. Causas: streaming/micro-lotes, cargas muito
  frequentes, partição granular demais, paralelismo alto na escrita.
- Mitigação: compactação periódica (reescrever partições em arquivos maiores),
  reduzir paralelismo de escrita, agrupar por partição antes de escrever,
  frequência de carga compatível com o volume.
- Formatos de tabela com log de transações acumulam versões antigas: rotina de
  limpeza (vacuum/expiração de snapshots) recupera espaço, mas **remove a
  possibilidade de voltar no tempo** além da retenção. É operação administrativa.
- Compactação e limpeza são escritas: em prod exigem aprovação e janela.

## Armadilhas comuns

- Partição por hora em tabela com pouco volume: milhares de arquivos minúsculos.
- Filtrar por expressão sobre a coluna de partição e ler a tabela inteira.
- Esquecer o filtro de partição em consulta exploratória numa engine cobrada
  por volume lido.
- Mudar a coluna de partição de uma tabela existente "no lugar": exige reescrita
  completa; trate como backfill.
- Skew: uma partição (ou valor de cluster) concentrando a maior parte dos dados.
- Partição por data local x consulta em UTC.

## Como o agente deve agir

1. Antes de consultar uma tabela grande, leia os metadados: colunas de
   partição/clustering, tamanho, número de arquivos/partições. Inclua filtro
   na coluna de partição sempre que possível.
2. Use EXPLAIN (ou dry-run de custo) para confirmar que o pruning aconteceu e
   estimar o volume lido antes de rodar.
3. Ao propor tabela nova, justifique partição (ou ausência dela) com volume e
   padrão de consulta; prefira não particionar tabela pequena.
4. Para diagnosticar small files, conte arquivos e tamanho médio por partição
   via metadados, sem ler os dados.
5. Compactação, limpeza de versões e mudança de partição são escrita/ADMIN:
   proponha, estime e peça aprovação.
