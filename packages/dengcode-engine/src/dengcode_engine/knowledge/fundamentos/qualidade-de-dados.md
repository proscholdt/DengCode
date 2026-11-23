# Qualidade de dados

## Quando usar / por que importa

Use sempre que criar, alterar ou investigar um pipeline. Testes de qualidade
são o contrato executável de uma tabela: dizem o que deve ser verdade em toda
execução. O **data diff antes/depois** é como o DengCode prova que uma mudança
fez só o que devia. Teste que nunca falha porque não testa nada é tão ruim
quanto nenhum teste.

## Dimensões de qualidade

| Dimensão | Pergunta | Teste típico |
| --- | --- | --- |
| Unicidade | a chave identifica uma linha só? | contagem por chave > 1 |
| Completude | campos obrigatórios estão preenchidos? | nulos em colunas obrigatórias |
| Validade | valores respeitam domínio, formato e faixa? | valores aceitos, regex, min/max |
| Consistência / integridade | relações batem entre tabelas? | órfãos (FK sem PK), somas que fecham |
| Exatidão | reflete a realidade/origem? | reconciliação com a origem ou total de controle |
| Frescor (pontualidade) | o dado está atualizado? | `MAX(data_carga)` vs agora |
| Volume | chegou a quantidade esperada? | contagem do dia vs média/intervalo histórico |

## Testes essenciais (SQL genérico)

Convenção: o teste **retorna as linhas problemáticas**; zero linhas = passou.

```sql
-- Unicidade
SELECT pedido_id, COUNT(*) AS n FROM silver_pedidos
GROUP BY pedido_id HAVING COUNT(*) > 1;

-- Não nulo
SELECT * FROM silver_pedidos WHERE cliente_id IS NULL;

-- Valores aceitos
SELECT status, COUNT(*) FROM silver_pedidos
WHERE status NOT IN ('aberto', 'pago', 'enviado', 'cancelado')
   OR status IS NULL
GROUP BY status;

-- Integridade referencial (órfãos)
SELECT p.cliente_id, COUNT(*) FROM silver_pedidos p
LEFT JOIN silver_clientes c ON c.cliente_id = p.cliente_id
WHERE c.cliente_id IS NULL AND p.cliente_id IS NOT NULL
GROUP BY p.cliente_id;

-- Faixa
SELECT * FROM silver_pedidos
WHERE valor_total < 0 OR data_pedido > CURRENT_DATE + INTERVAL '1' DAY;

-- Frescor (retorna linha se atrasado)
SELECT MAX(_ingerido_em) AS ultimo FROM bronze_pedidos
HAVING MAX(_ingerido_em) < CURRENT_TIMESTAMP - INTERVAL '6' HOUR;

-- Volume: dia atual fora de ±50% da média dos 28 dias anteriores
WITH d AS (SELECT data_pedido, COUNT(*) AS n FROM silver_pedidos GROUP BY 1),
     ref AS (SELECT AVG(n) AS media FROM d
             WHERE data_pedido >= CURRENT_DATE - INTERVAL '29' DAY
               AND data_pedido <  CURRENT_DATE - INTERVAL '1' DAY)
SELECT d.* FROM d CROSS JOIN ref
WHERE d.data_pedido = CURRENT_DATE - INTERVAL '1' DAY
  AND d.n NOT BETWEEN 0.5 * ref.media AND 1.5 * ref.media;
-- Atenção: dia sem nenhuma linha não aparece em "d"; teste a ausência à parte.
```

Nota: `NOT IN` com subconsulta que contém NULL retorna zero linhas — prefira
`NOT EXISTS` ou `LEFT JOIN ... IS NULL` para integridade.

## Severidade e onde testar

- **Bloqueante (erro)**: unicidade de chave, não nulo de chave, integridade em
  modelos de consumo. Falhou → não publica.
- **Alerta (warn)**: volume, frescor, distribuição. Falhou → investiga.
- Teste na **entrada** (fonte/bronze: frescor, volume, schema), no **silver**
  (unicidade, domínio, integridade) e no **gold** (regras de negócio,
  reconciliação com o silver).
- Limiar por porcentagem para dados sujos conhecidos (`< 0,1%` nulos) é melhor
  que desligar o teste.

## Data diff antes/depois

Comparar a versão atual de uma tabela com a versão nova (construída em schema
de dev/temporário) é a verificação principal de mudança em pipeline:

1. **Contagem** de linhas de cada lado.
2. **Chaves**: duplicadas em cada lado; só no antigo; só no novo.
3. **Por coluna** (nas chaves em comum): quantas linhas mudaram de valor.
4. **Nulos** por coluna, antes x depois.
5. **Numéricas**: min, max, média, soma.
6. **Categóricas**: top valores e frequências.

```sql
-- Chaves só de um lado e linhas alteradas, por chave
SELECT
  SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS so_no_novo,
  SUM(CASE WHEN n.id IS NULL THEN 1 ELSE 0 END) AS so_no_antigo,
  SUM(CASE WHEN a.id IS NOT NULL AND n.id IS NOT NULL
            AND a.valor IS DISTINCT FROM n.valor THEN 1 ELSE 0 END) AS valor_mudou
FROM atual a
FULL OUTER JOIN nova n ON a.id = n.id;
```

Interpretação: a mudança pedida deve explicar **cada** diferença. Coluna nova
adicionada → contagem e chaves idênticas, colunas antigas sem mudanças, coluna
nova com nulos dentro do esperado. Qualquer outra diferença é sinal de efeito
colateral.

Use `IS DISTINCT FROM` (ou equivalente) para comparar: `a = b` com NULL não é
verdadeiro nem falso, e esconde mudanças de/para NULL.

## Armadilhas comuns

- Teste de unicidade na coluna errada (chave composta testada só em parte).
- Teste de aceitação de valores que ignora NULL.
- Comparar floats com igualdade exata no diff (use tolerância ou arredonde).
- Testar só a amostra do dev e concluir sobre prod.
- Desabilitar o teste que falhou em vez de entender a causa.
- Teste de volume sem sazonalidade (fins de semana, feriados disparam alertas).

## Como o agente deve agir

1. Antes de alterar um modelo, liste os testes existentes dele e rode-os; anote
   o estado inicial (passa/falha) para não atribuir falha antiga à mudança.
2. Modelo novo ou coluna-chave nova: adicione ao menos unicidade e não nulo da
   chave e integridade com as dimensões.
3. Após a mudança, rode os testes do modelo e dos dependentes diretos, depois o
   data diff contra a versão atual. Relate as diferenças com explicação.
4. Ao mostrar exemplos de linhas com problema, use amostras pequenas; dados
   pessoais aparecem mascarados (ver `lgpd`). Prefira agregados e contagens.
5. Testes de qualidade são consultas de leitura; mantenha-as com `LIMIT` ao
   listar linhas problemáticas em tabelas grandes.
