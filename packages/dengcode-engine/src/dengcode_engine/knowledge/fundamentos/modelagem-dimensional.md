# Modelagem dimensional

## Quando usar / por que importa

Use ao desenhar ou alterar a camada de consumo (marts, gold, tabelas de BI):
fatos com métricas e dimensões com contexto descritivo. O modelo dimensional
torna as consultas previsíveis (estrela: fato no centro, dimensões em volta),
evita dupla contagem e permite que várias áreas usem as mesmas dimensões. A
maioria dos bugs de números "errados" em relatório vem de grão mal definido ou
de join que multiplica linhas — é isso que este documento ajuda a evitar.

## Conceitos centrais

- **Grão**: o que uma linha do fato representa, em uma frase
  ("uma linha por item de pedido", "um saldo por conta por dia"). Defina o grão
  **antes** das colunas. Toda coluna do fato tem que ser verdadeira nesse grão.
- **Fato**: eventos ou medições. Colunas = chaves para dimensões + medidas
  numéricas + dimensões degeneradas (ex.: número do pedido, sem tabela própria).
- **Tipos de fato**:
  - *transacional*: um evento por linha (venda, clique). O mais comum.
  - *snapshot periódico*: estado a cada período (saldo diário, estoque mensal).
  - *snapshot acumulado*: um processo com marcos (pedido: criado, pago,
    enviado, entregue), linha atualizada conforme avança.
  - *fato sem fato* (factless): registra ocorrência sem medida (presença,
    elegibilidade); conta-se linhas.
- **Dimensão**: entidade descritiva (cliente, produto, data, loja). Larga,
  desnormalizada, com atributos legíveis para filtro e agrupamento.
- **Chave substituta (surrogate key)**: identificador gerado no warehouse,
  sem significado de negócio. Necessária quando a dimensão guarda histórico
  (SCD tipo 2: mesma chave natural, várias versões) ou junta várias origens.
  Pode ser sequencial ou hash determinístico da chave natural (+ versão).
  Hash determinístico facilita reprocessamento idempotente.
- **Chave natural / de negócio**: o identificador da origem (CPF, código do
  produto). Mantenha-a na dimensão para rastreabilidade e merge.
- **Dimensões conformadas**: a mesma dimensão (mesma chave, mesmos atributos,
  mesmo significado) usada por vários fatos. É o que permite comparar vendas e
  devoluções por produto. Matriz de barramento: fatos x dimensões compartilhadas.
- **Membro desconhecido**: linha especial na dimensão (ex.: chave `-1`,
  "Não informado") para fatos cuja dimensão não chegou ou é nula. Evita FK nula
  e perda de linhas em `INNER JOIN`.
- **Dimensão de data**: tabela própria com um registro por dia (dia útil,
  feriado, semana fiscal). Não derive tudo da coluna de data em cada consulta.
- **Junk / mini-dimensão**: agrupa flags de baixa cardinalidade, ou atributos
  que mudam muito, para não inchar a dimensão principal.
- **Bridge**: resolve relacionamentos muitos-para-muitos (conta com vários
  titulares), geralmente com fator de ponderação.

## Aditividade das medidas

| Tipo | Pode somar em | Exemplos | Cuidado |
| --- | --- | --- | --- |
| Aditiva | todas as dimensões | valor da venda, quantidade | — |
| Semiaditiva | algumas (não no tempo) | saldo, estoque, nº de clientes ativos | no tempo use último valor ou média, nunca soma |
| Não aditiva | nenhuma | percentuais, preço unitário, médias, razões | guarde numerador e denominador; calcule a razão no fim |

```sql
-- Errado: soma de saldo diário ao longo do mês
SELECT conta_sk, SUM(saldo) FROM fato_saldo_diario GROUP BY conta_sk;

-- Certo: saldo de fechamento do mês
SELECT conta_sk, mes, saldo
FROM (
  SELECT f.conta_sk, d.ano_mes AS mes, f.saldo,
         ROW_NUMBER() OVER (PARTITION BY f.conta_sk, d.ano_mes
                            ORDER BY d.data DESC) AS rn
  FROM fato_saldo_diario f
  JOIN dim_data d ON d.data_sk = f.data_sk
) s
WHERE rn = 1;
```

## Regras de decisão

- Escolha o **grão mais atômico disponível**; agregados são derivados depois.
  Grão agregado demais impede perguntas futuras e não pode ser "desagregado".
- Um fato, um grão. Medidas de grãos diferentes (cabeçalho do pedido x item)
  vão para fatos diferentes, ou o frete do cabeçalho é rateado entre itens com
  regra explícita.
- Estrela (dimensões desnormalizadas) é o padrão; floco de neve só quando há
  motivo concreto (dimensão enorme com hierarquia reutilizada).
- Atributo descritivo que muda e precisa de histórico → SCD tipo 2 (ver `scd`).
  Sem necessidade de histórico → tipo 1.
- Chave substituta: use quando houver histórico ou múltiplas origens; caso
  contrário a chave natural estável pode bastar.
- Métrica derivada (ticket médio, margem %) não se armazena como soma; guarde os
  componentes.

## Armadilhas comuns

- **Fan-out**: juntar fato com tabela de grão mais fino (ou dimensão com chave
  duplicada) multiplica linhas e infla somas. Sempre verifique unicidade da
  chave da dimensão antes do join.
- **Chasm trap**: dois fatos ligados à mesma dimensão juntados diretamente.
  Agregue cada fato ao grão comum primeiro, depois junte.
- Join com dimensão SCD2 sem filtrar a versão vigente na data do fato.
- FK nula no fato + `INNER JOIN` → linhas somem do relatório silenciosamente.
- Mudar o grão de um fato existente sem avisar o downstream.
- Dimensão "conformada" com significados diferentes por fato (ex.: "cliente
  ativo" definido de dois jeitos).
- Colocar texto livre e de alta cardinalidade no fato.

## Como o agente deve agir

1. Antes de alterar um fato, **descubra e declare o grão**: leia a definição,
   descrição/comentários e teste a unicidade da chave candidata:
   ```sql
   SELECT pedido_id, item_seq, COUNT(*) AS n
   FROM fato_itens_pedido GROUP BY 1, 2 HAVING COUNT(*) > 1 LIMIT 20;
   ```
2. Para cada join novo, verifique a unicidade da chave do lado dimensão e compare
   contagem de linhas antes/depois do join (deve ser igual em join fato→dimensão).
3. Verifique órfãos: fatos sem dimensão correspondente (ver `qualidade-de-dados`).
4. Ao adicionar medida, classifique-a (aditiva/semi/não aditiva) e documente.
5. Rode impact analysis: quem consome o fato/dimensão e quais colunas.
6. Valide com data diff: contagem de linhas igual, somas das medidas aditivas
   iguais (ou com diferença explicada), sem chaves duplicadas novas.
7. Nunca mude grão, chave ou semântica de coluna existente sem aprovação
   explícita; prefira coluna nova.
