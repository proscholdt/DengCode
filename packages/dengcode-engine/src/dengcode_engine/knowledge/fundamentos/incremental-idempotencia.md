# Cargas incrementais e idempotência

## Quando usar / por que importa

Use ao criar ou alterar qualquer carga que não reconstrói a tabela inteira a
cada execução. Incremental reduz custo e tempo, mas introduz estado: o que já
foi carregado, até onde, e o que acontece se a execução falhar no meio ou rodar
duas vezes. **Idempotência** — rodar N vezes com a mesma entrada produz o mesmo
resultado que rodar uma vez — é a propriedade que torna retry, backfill e
recuperação seguros. Uma carga não idempotente gera duplicidade silenciosa.

## Estratégias

| Estratégia | Como funciona | Idempotente? | Bom para |
| --- | --- | --- | --- |
| Full refresh | recria a tabela inteira | sim | tabelas pequenas/médias, lógica que muda |
| Append | insere linhas novas | **não**, por si só | logs imutáveis com dedup posterior |
| Merge/upsert por chave | atualiza se existe, insere se não | sim (com fonte deduplicada) | entidades com atualização |
| Delete + insert por chave/janela | apaga o recorte e reinsere | sim | lotes por data, quando merge é caro |
| Sobrescrever partição | troca partições inteiras | sim | fatos por data, particionados |

Regra prática: prefira **sobrescrever partições** ou **merge por chave**; use
append só quando a tabela tiver deduplicação garantida a jusante.

## Watermark (marca d'água)

- Guarda "até onde já carreguei": `MAX(updated_at)` do destino, ou um registro
  de controle por tabela.
- Filtro na origem: `updated_at > :watermark` — mas com **janela de segurança**
  (lookback): `updated_at > :watermark - INTERVAL '2' HOUR` (ajuste ao atraso
  real). Combinado com merge, reler linhas é inofensivo.
- Atualize o watermark **só depois** do commit da carga, na mesma transação se
  possível.
- Use coluna monotônica e confiável: data de alteração mantida pelo sistema,
  posição de log, sequência. Data de criação não captura updates.
- Cuidado com fuso e precisão: comparar `TIMESTAMP` local com UTC, ou truncar
  milissegundos, perde ou repete linhas na borda.

```sql
-- Origem deduplicada (última versão por chave) + merge
MERGE INTO silver_pedidos t
USING (
  SELECT * FROM (
    SELECT s.*, ROW_NUMBER() OVER (PARTITION BY pedido_id
                                   ORDER BY updated_at DESC, _ingerido_em DESC) AS rn
    FROM bronze_pedidos s
    WHERE updated_at > :watermark - INTERVAL '2' HOUR
  ) x WHERE rn = 1
) s
ON t.pedido_id = s.pedido_id
WHEN MATCHED AND s.updated_at >= t.updated_at THEN
  UPDATE SET status = s.status, valor = s.valor, updated_at = s.updated_at
WHEN NOT MATCHED THEN
  INSERT (pedido_id, status, valor, updated_at)
  VALUES (s.pedido_id, s.status, s.valor, s.updated_at);
```

Pontos do exemplo: a fonte é deduplicada (merge com chave repetida na fonte
falha ou fica não determinístico); o `AND s.updated_at >= t.updated_at` impede
que um dado mais velho sobrescreva um mais novo; o desempate (`_ingerido_em`)
torna a ordenação determinística.

## Deduplicação

- Defina a **chave** e a **regra de vencedor** (mais recente por
  `updated_at`, depois por posição de ingestão). Sem desempate, o resultado
  muda entre execuções.
- `ROW_NUMBER()` com `ORDER BY` completo, não `DISTINCT` (que só remove linhas
  idênticas em todas as colunas).
- Duplicatas exatas (reentrega do mesmo arquivo) e duplicatas lógicas (mesma
  chave, valores diferentes) são problemas diferentes; trate ambos.

## Dados atrasados (late-arriving)

- **Fatos atrasados**: evento de ontem chega hoje. Partição por data do evento
  exige reprocessar janelas passadas (lookback de N dias) ou merge por chave.
- **Dimensão atrasada**: fato chega antes da dimensão. Use membro
  "desconhecido" e corrija depois, ou crie dimensão inferida (linha mínima
  com a chave natural, completada quando a dimensão chegar).
- Meça o atraso real (`_ingerido_em - data_evento`, percentil 99) para
  dimensionar o lookback, em vez de chutar.

## Regras de decisão

- Tabela pequena ou lógica instável → full refresh. Otimize depois.
- Incremental exige: chave única definida, coluna de alteração confiável,
  estratégia para deletes (ver `cdc`) e plano de full refresh.
- Toda lógica incremental precisa produzir o **mesmo resultado que o full
  refresh** para o mesmo período. Isso é testável.
- Nada de `NOW()`/`CURRENT_DATE` dentro da transformação para decidir o que
  entra: use parâmetros da execução (data lógica), senão reprocessar o passado
  dá outro resultado.
- Operação não atômica (apagar e depois inserir em transações separadas) →
  janela em que o consumidor vê dados faltando. Use transação ou troca atômica.

## Armadilhas comuns

- Append com retry: a execução falha após inserir metade, o retry insere tudo
  de novo.
- Watermark atualizado antes da carga terminar.
- Mudança de lógica aplicada só às linhas novas: a tabela fica com duas regras
  misturadas. Mudou a regra → backfill (ver `backfill`).
- Coluna nova num modelo incremental: linhas antigas ficam NULL até um full
  refresh ou backfill.
- Merge sem condição de versão: reprocessar dados antigos regride o estado.

## Como o agente deve agir

1. Identifique estratégia, chave única, coluna de watermark e lookback do modelo.
2. Verifique unicidade da chave no destino e na fonte do lote:
   `SELECT chave, COUNT(*) FROM ... GROUP BY 1 HAVING COUNT(*) > 1`.
3. Teste de idempotência em dev: rode o incremental duas vezes seguidas; data
   diff entre execuções deve ser vazio (mesma contagem, sem chaves duplicadas).
4. Teste de equivalência: para uma janela, compare incremental x full refresh
   (data diff por chave).
5. Ao adicionar coluna ou mudar lógica, avise que linhas históricas precisam de
   backfill/full refresh e estime o custo antes de propor.
6. Em prod, merge/delete/overwrite são escrita: exigem aprovação e nunca são
   executados como "teste".
