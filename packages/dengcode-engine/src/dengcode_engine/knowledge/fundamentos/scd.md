# SCD — Dimensões que mudam lentamente

## Quando usar / por que importa

Use quando atributos de uma dimensão mudam ao longo do tempo (endereço do
cliente, categoria do produto, gerente da loja) e é preciso decidir se a
análise vê o valor **atual** ou o valor **vigente na data do fato**. A escolha
errada reescreve o passado (vendas de 2023 aparecem com a região de hoje) ou
gera duplicidade em joins. SCD tipo 2 mal implementado é uma das maiores fontes
de fan-out e de bugs em reprocessamento.

## Tipos

| Tipo | Comportamento | Quando usar |
| --- | --- | --- |
| 0 | valor original nunca muda | data de nascimento, data de cadastro |
| 1 | sobrescreve; sem histórico | correção de erro, atributo sem valor analítico histórico |
| 2 | nova linha por versão, com vigência | análise "como era na época" (região, segmento, preço de tabela) |
| 3 | coluna "valor anterior" ao lado do atual | só a mudança mais recente importa (reorganização única) |
| 4 | tabela atual pequena + tabela de histórico separada | dimensão grande com atributos que mudam muito |
| 6 | 1+2+3: linhas versionadas (2) + coluna "atual" sobrescrita em todas as versões (1) | precisa de "como era" e "como é hoje" no mesmo join |

A escolha é **por atributo**: na mesma dimensão, nome pode ser tipo 1 e
segmento tipo 2. Documente quais colunas disparam nova versão.

## Colunas de vigência (tipo 2)

- `sk` (chave substituta da versão), `chave_natural`
- `valido_de` (inclusivo), `valido_ate` (exclusivo; versão atual = data
  sentinela `9999-12-31` ou NULL — escolha um e seja consistente)
- `atual` (booleano), opcional `versao` (inteiro), `hash_atributos`
- Intervalo **meio-aberto** `[valido_de, valido_ate)`: o fim de uma versão é
  igual ao início da próxima. Evita lacunas e sobreposição de 1 dia/segundo.

Invariantes que devem sempre valer:

- exatamente **uma** linha `atual` por chave natural;
- versões da mesma chave **não se sobrepõem** e não têm lacunas (salvo regra);
- `valido_de < valido_ate`.

```sql
-- Join de fato com a versão vigente na data do evento
SELECT f.*, d.segmento
FROM fato_vendas f
JOIN dim_cliente d
  ON d.cliente_id = f.cliente_id
 AND f.data_venda >= d.valido_de
 AND f.data_venda <  d.valido_ate;   -- com sentinela 9999-12-31
```

Melhor ainda: resolver a `sk` da versão **na carga do fato** e gravar só
`cliente_sk` no fato. O join no consumo fica por igualdade simples.

## Carga tipo 2 (padrão em duas etapas)

1. Detecte mudança comparando hash dos atributos rastreados entre a origem
   (já deduplicada por chave natural) e a versão atual.
2. Feche a versão atual das chaves que mudaram e insira a nova versão.

```sql
-- 1) fechar versões alteradas
UPDATE dim_cliente d
SET valido_ate = s.data_mudanca, atual = FALSE
FROM stg_cliente s
WHERE d.cliente_id = s.cliente_id
  AND d.atual
  AND d.hash_atributos <> s.hash_atributos;

-- 2) inserir novas versões (chaves novas ou alteradas)
INSERT INTO dim_cliente (cliente_sk, cliente_id, segmento, hash_atributos,
                         valido_de, valido_ate, atual)
SELECT s.cliente_sk_nova, s.cliente_id, s.segmento, s.hash_atributos,
       s.data_mudanca, DATE '9999-12-31', TRUE
FROM stg_cliente s
LEFT JOIN dim_cliente d ON d.cliente_id = s.cliente_id AND d.atual
WHERE d.cliente_id IS NULL;
```

Em engines com `MERGE` é possível fazer em uma instrução (truque de unir a
origem duas vezes: uma para fechar, outra para inserir), mas as duas etapas numa
transação são mais legíveis e fáceis de testar.

## Armadilhas de reprocessamento

- **Data de vigência = data de carga**: reprocessar gera vigências diferentes a
  cada execução; não é idempotente. Prefira a data do evento/alteração na origem
  (`updated_at`, posição do CDC). Se só existe data de carga, registre-a e não
  reprocesse o passado sem reconstruir a dimensão inteira.
- **Rodar a mesma carga duas vezes** não pode criar versão nova: só crie versão
  se o hash mudou.
- **Várias mudanças da mesma chave no mesmo lote**: deduplicar para a última
  perde histórico; para manter, ordene por data de alteração e gere versões em
  sequência (`LEAD(data_mudanca) OVER (PARTITION BY chave ORDER BY data_mudanca)`
  como `valido_ate`).
- **Dados atrasados** (mudança antiga chegando depois): exige "inserir no meio"
  — dividir a versão que cobre aquela data. Raramente vale fazer incremental;
  reconstruir a história da chave afetada é mais seguro.
- **Backfill de fatos** depois de uma reconstrução da dimensão: se as `sk`
  mudaram, os fatos antigos apontam para chaves inexistentes. Use `sk`
  determinística (hash de chave natural + `valido_de`) para estabilidade.
- Hash calculado sobre colunas com NULL: `concat` com NULL pode virar NULL ou
  ignorar o valor, dependendo da engine. Use `COALESCE` e um separador explícito.
- Mudanças só de caixa/espaço gerando versões espúrias: normalize antes do hash.
- Delete na origem: decida se fecha a versão (`valido_ate`), marca flag
  `excluido` ou ignora. Nunca apague versões históricas.

## Como o agente deve agir

1. Antes de mexer numa dimensão, identifique o tipo SCD de cada atributo
   (colunas de vigência presentes? descrição do modelo?).
2. Valide as invariantes na tabela atual e na nova:
   ```sql
   -- mais de uma versão atual
   SELECT cliente_id FROM dim_cliente WHERE atual
   GROUP BY 1 HAVING COUNT(*) > 1;
   -- sobreposição de vigências
   SELECT a.cliente_id FROM dim_cliente a JOIN dim_cliente b
     ON a.cliente_id = b.cliente_id AND a.cliente_sk <> b.cliente_sk
    AND a.valido_de < b.valido_ate AND b.valido_de < a.valido_ate;
   ```
3. Teste idempotência: rode a carga duas vezes em dev com a mesma entrada; o
   data diff entre as execuções deve ser vazio.
4. Verifique fan-out nos fatos: contagem do fato antes/depois do join com a
   dimensão deve ser idêntica.
5. Converter tipo 1 → tipo 2 (ou o contrário) muda o grão da dimensão: trate
   como mudança de contrato, com impact analysis e aprovação.
6. Nunca rode reconstrução completa de dimensão em prod sem aprovação; em dev,
   compare contagem de chaves naturais e de versões atuais antes/depois.
