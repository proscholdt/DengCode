# Backfill

## Quando usar / por que importa

Backfill é reprocessar um período passado: carregar histórico de uma tabela
nova, aplicar uma regra corrigida às linhas antigas, preencher coluna nova ou
recuperar dias que falharam. É a operação de maior risco rotineiro em
engenharia de dados: escreve muito, custa caro, afeta tudo que está a jusante e,
se a carga não for idempotente, duplica dados. Planeje antes de executar.

## Estratégias

| Estratégia | Como | Quando |
| --- | --- | --- |
| Full refresh | reconstrói a tabela inteira | tabela cabe no orçamento; lógica mudou em tudo |
| Por janelas (partições) | reprocessa intervalo a intervalo (dia, mês) | tabelas grandes, particionadas por data |
| Por chave | reprocessa um conjunto de chaves afetadas | bug restrito a clientes/produtos específicos |
| Tabela paralela + troca | constrói `tabela_nova`, valida, troca atomicamente (rename/view) | tabela crítica; precisa validar antes de expor |
| Coluna nova com default | preenche só a coluna nova via update/merge | adição de coluna derivável de dados existentes |

Regra prática: para tabela crítica de consumo, **construa ao lado, valide e
troque**. Sobrescrever no lugar só quando o reprocessamento for idempotente e
o consumidor tolerar a janela de atualização.

## Planejamento (checklist)

- **Motivo e escopo**: qual período, quais chaves, quais colunas mudam.
- **Pré-requisitos**: a carga é idempotente? (ver `incremental-idempotencia`).
  Os dados de origem do período ainda existem (retenção do bronze)?
- **Parâmetro de data lógica**: a transformação usa a data da execução
  (parâmetro), não o relógio. Sem isso, reprocessar janela antiga produz
  resultado errado.
- **Dependências a montante**: as dimensões/tabelas de entrada estão corretas
  para aquele período? (SCD2: a versão vigente na época existe?)
- **Impacto a jusante**: quais modelos, dashboards e exportações leem a
  tabela; eles precisam ser reprocessados também, na ordem do DAG.
- **Custo**: estime bytes lidos/escritos ou tempo de computação para uma
  janela, multiplique pelo número de janelas. Compare com o teto (`cost_limit`).
- **Concorrência**: a carga diária regular vai rodar ao mesmo tempo? Pause-a ou
  garanta que as janelas não se sobrepõem.
- **Ordem**: do mais antigo para o mais recente quando há estado acumulado
  (SCD, snapshots acumulados, saldos); em paralelo só se as janelas forem
  independentes.
- **Plano de volta**: snapshot/cópia/time travel da tabela antes, ou tabela
  paralela que permite descartar.
- **Comunicação**: consumidores sabem que os números do período vão mudar.

## Janelas

- Escolha a janela pelo particionamento físico (sobrescrever partição inteira é
  atômico e barato) e pelo limite de custo por execução.
- Janelas sem sobreposição e cobrindo o período inteiro; registre quais
  terminaram (para retomar após falha).
- Lookback de dados atrasados também se aplica: a janela de D pode precisar ler
  a origem de D até D+k.

```sql
-- Padrão delete+insert por janela, numa transação
BEGIN;
DELETE FROM fato_vendas
WHERE data_venda >= DATE '2025-01-01' AND data_venda < DATE '2025-02-01';
INSERT INTO fato_vendas
SELECT ... FROM silver_vendas
WHERE data_venda >= DATE '2025-01-01' AND data_venda < DATE '2025-02-01';
COMMIT;
```

Use intervalos meio-abertos (`>=` início, `<` fim) para não perder nem repetir
o último dia/instante.

## Verificação

Antes de expor o resultado (ou logo após, se for no lugar):

- **Contagem por janela** antes x depois; diferenças devem ter explicação.
- **Chaves**: sem duplicatas novas; chaves só de um lado listadas e explicadas.
- **Medidas aditivas**: soma por janela antes x depois; para correção de bug,
  a diferença deve ficar restrita ao que o bug afetava.
- **Nulos** por coluna; coluna nova preenchida no período inteiro.
- **Continuidade**: nenhum dia faltando
  (`SELECT data, COUNT(*) ... GROUP BY data` comparado a um calendário).
- **Downstream**: rodar testes dos modelos dependentes após reprocessá-los.

```sql
-- Diferença de contagem e soma por mês, versão atual x nova
SELECT COALESCE(a.mes, n.mes) AS mes,
       a.linhas AS linhas_atual, n.linhas AS linhas_nova,
       a.total  AS total_atual,  n.total  AS total_nova
FROM (SELECT mes, COUNT(*) linhas, SUM(valor) total FROM atual GROUP BY mes) a
FULL OUTER JOIN
     (SELECT mes, COUNT(*) linhas, SUM(valor) total FROM nova GROUP BY mes) n
  ON a.mes = n.mes
WHERE a.linhas IS DISTINCT FROM n.linhas OR a.total IS DISTINCT FROM n.total;
```

## Armadilhas comuns

- Backfill de append não idempotente: duplica o período.
- Esquecer o downstream: a tabela está corrigida, os agregados não.
- Usar a dimensão **atual** para reprocessar fatos antigos (reescreve o passado).
- Origem com retenção menor que o período: bronze já não tem os dados; o
  backfill "funciona" e apaga histórico.
- Disparar todas as janelas em paralelo e estourar cota/custo ou travar a origem.
- Reprocessar uma partição parcial (sobrescrever o mês inteiro com só 10 dias).
- Fuso horário: janela em UTC x partição em horário local desloca 3 horas.

## Como o agente deve agir

1. Produza um **plano escrito** antes de executar: escopo, estratégia, janelas,
   ordem, custo estimado, impacto downstream, plano de volta. Peça aprovação.
2. Rode primeiro **uma janela** em dev/schema temporário e compare com a atual
   (data diff). Só então proponha o resto.
3. Use EXPLAIN/dry-run para estimar custo de uma janela quando a engine suporta.
4. Em prod, cada escrita passa pela política (ASK/DENY); nunca encadeie janelas
   sem aprovação do plano completo.
5. Ao final, relate por janela: linhas, somas, diferenças e testes executados.
