# CDC — Captura de dados alterados

## Quando usar / por que importa

Use quando for replicar uma base operacional (OLTP) para o ambiente analítico
mantendo inserts, updates **e deletes**, sem copiar a tabela inteira a cada
vez. CDC bem aplicado dá frescor e baixo custo; mal aplicado deixa registros
apagados vivos no warehouse, aplica updates fora de ordem ou duplica linhas.

## Abordagens

| | Baseado em log | Baseado em consulta |
| --- | --- | --- |
| Como | lê o log de transações do banco (WAL/redo/binlog) | consulta periódica por `updated_at > watermark` |
| Deletes | capturados | **não** capturados (a linha sumiu) — exige soft delete na origem ou comparação completa |
| Updates intermediários | todos (cada versão) | só o estado no momento da consulta |
| Carga na origem | baixa | consultas recorrentes; precisa de índice na coluna |
| Requisitos | permissão/config no banco, slot ou leitura de log, retenção do log | coluna de alteração confiável |
| Ordem | posição no log (LSN/SCN/offset) | `updated_at` (pode empatar ou ter relógio torto) |

Outras variantes: triggers gravando tabela de auditoria (captura deletes, pesa
na origem) e snapshot diff (compara cópia completa de hoje x ontem; caro, mas
captura deletes sem acesso ao log).

## Formato típico de um evento

- `op`: `c`/`u`/`d` (ou `I`/`U`/`D`), às vezes `r` para leitura do snapshot
  inicial;
- `before` / `after`: imagem da linha antes e depois;
- posição: LSN/offset/sequência + timestamp da transação;
- chave primária da linha.

**Tombstone**: evento que marca a exclusão de uma chave (às vezes um registro
com valor nulo após o evento de delete). Serve para compactação do log e para
o consumidor saber que a chave deve ser removida. Não confundir com linha
"vazia" a ser inserida.

## Aplicando no destino (upsert por chave)

1. **Bronze**: grave os eventos como chegaram (append), com posição e
   timestamp. É o histórico auditável.
2. **Silver**: estado atual por chave = último evento por chave, ordenado pela
   **posição no log** (não pelo horário de chegada).
3. Aplique com merge: `d` → delete (ou soft delete), `c/u` → upsert.

```sql
MERGE INTO silver_clientes t
USING (
  SELECT * FROM (
    SELECT e.*, ROW_NUMBER() OVER (PARTITION BY cliente_id
                                   ORDER BY log_pos DESC) AS rn
    FROM bronze_clientes_cdc e
    WHERE log_pos > :ultima_pos_aplicada
  ) x WHERE rn = 1
) s
ON t.cliente_id = s.cliente_id
WHEN MATCHED AND s.op = 'd' AND s.log_pos > t.log_pos THEN DELETE
WHEN MATCHED AND s.log_pos > t.log_pos THEN
  UPDATE SET nome = s.nome, email = s.email, log_pos = s.log_pos
WHEN NOT MATCHED AND s.op <> 'd' THEN
  INSERT (cliente_id, nome, email, log_pos)
  VALUES (s.cliente_id, s.nome, s.email, s.log_pos);
```

- Guarde `log_pos` no destino: torna a aplicação idempotente e impede que um
  evento antigo reaplicado regrida o estado.
- Soft delete (`excluido_em`) em vez de `DELETE` quando auditoria ou SCD2
  precisam saber que a chave existiu.
- Snapshot inicial + stream: carregue o snapshot, registre a posição do log em
  que ele foi tirado e aplique o stream a partir dela. Sobreposição é inofensiva
  se o merge for por posição.

## Ordenação

- A ordem correta é a da **transação na origem** (posição do log). Horário de
  chegada no pipeline não é confiável (retries, paralelismo, partições).
- Ordem só é garantida **por chave** (ou por partição do transporte). Não
  assuma ordem global.
- Empates de `updated_at` em CDC por consulta: adicione desempate determinístico
  (PK, sequência) e lookback.
- Transações multi-tabela: aplicar tabelas independentemente pode expor estado
  inconsistente momentâneo (pedido sem itens). Aceite ou aplique por lote de
  transação.

## Mudanças de schema na origem

- Coluna nova: bronze deve aceitar (schema flexível ou evolução); silver
  decide se propaga.
- Coluna removida/renomeada/tipo alterado: quebra contratos; alerte.
- Mudança de chave primária na origem invalida o upsert por chave.

## Armadilhas comuns

- CDC por consulta sem tratar deletes: registros excluídos ficam para sempre.
- Ordenar por horário de ingestão em vez de posição no log.
- Merge com várias linhas da mesma chave na fonte (sem dedup do lote).
- `UPDATE` com imagem parcial (só colunas alteradas): sobrescrever as outras com
  NULL. Verifique se o evento traz a linha completa.
- Retenção do log menor que o atraso do consumidor: perda de eventos; exige novo
  snapshot.
- Slot de replicação abandonado segurando log e enchendo o disco da origem.
- Hard delete no silver apagando chave que fatos ainda referenciam.

## Como o agente deve agir

1. Identifique o tipo de CDC (há coluna `op`, posição de log, `before/after`?).
2. Verifique se deletes são tratados: existe caminho para `op = 'd'` ou soft
   delete? Se for CDC por consulta, diga explicitamente que deletes não são
   capturados.
3. Confirme a coluna de ordenação e o desempate; procure empates:
   `SELECT chave, log_pos, COUNT(*) FROM ... GROUP BY 1, 2 HAVING COUNT(*) > 1`.
4. Valide o estado: uma linha por chave no silver; contagem de chaves vivas
   comparada à origem (contagem simples na origem é leitura, permitida).
5. Teste reaplicação: aplicar o mesmo lote duas vezes não muda o resultado.
6. Nunca configure slots/replicação ou altere parâmetros da origem: é operação
   administrativa, fora do escopo sem aprovação explícita.
