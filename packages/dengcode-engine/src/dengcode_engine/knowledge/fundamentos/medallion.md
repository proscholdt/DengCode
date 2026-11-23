# Arquitetura medallion (bronze, silver, gold)

## Quando usar / por que importa

Use para organizar um lakehouse/warehouse em camadas de refinamento crescente:
**bronze** (cru), **silver** (limpo e conformado), **gold** (pronto para
consumo). O valor não está nos nomes, e sim nos **contratos** entre camadas:
cada camada promete algo a quem a lê. Quando o agente sabe em que camada está,
sabe que tipo de mudança é aceitável ali e o que precisa ser validado.

## O que pertence a cada camada

### Bronze (raw / landing)

- Cópia fiel da origem, **append-only**, com o mínimo de transformação.
- Colunas técnicas: `_ingerido_em`, `_arquivo_origem` / `_lote_id`,
  `_offset`/posição de CDC, `_hash_linha` opcional.
- Schema da origem (frequentemente tudo como texto, ou semiestruturado).
- Permite reprocessar silver e gold do zero. Retenção definida (e alinhada à
  LGPD: bronze costuma ter PII crua — ver `lgpd`).
- Não pertence: regras de negócio, deduplicação destrutiva, joins.

### Silver (clean / conformed)

- Tipagem correta (datas, decimais), nomes padronizados, valores normalizados.
- **Deduplicação** e uma linha por chave de negócio no grão da entidade (ou o
  histórico explícito, ex.: SCD2).
- Aplicação de CDC (upsert/delete por chave), tratamento de nulos e defaults.
- Conformação entre origens (mesma entidade de dois sistemas com a mesma chave).
- Pseudonimização/mascaramento de PII que não é necessária adiante.
- Testes de qualidade fortes: unicidade, não nulo, domínios, integridade.
- Não pertence: agregações para um relatório específico, métricas de negócio
  com definição de área.

### Gold (curated / marts)

- Modelos de consumo: fatos e dimensões (ver `modelagem-dimensional`),
  agregados, tabelas largas para BI, features.
- Métricas com definição de negócio documentada e dono.
- Estável: é contrato com consumidores externos (dashboards, APIs, times).
- Não pertence: limpeza de dados crus (se o gold limpa, o silver está falhando).

## Contratos entre camadas

Um contrato declara, para cada tabela publicada:

- **schema**: colunas, tipos, nulabilidade;
- **grão e chave**: o que é uma linha; chave única;
- **semântica**: definição de cada coluna/métrica;
- **frescor (SLA)**: até quando os dados de D estão disponíveis;
- **garantias de qualidade**: testes que sempre passam;
- **política de mudança**: aditiva (coluna nova) é compatível; renomear, mudar
  tipo, mudar grão ou remover coluna é **quebra** e exige versão/comunicação.

| Mudança | Compatível? |
| --- | --- |
| adicionar coluna nullable | sim |
| ampliar tipo (INT → BIGINT, VARCHAR(50) → VARCHAR(100)) | geralmente sim |
| renomear/remover coluna | não |
| mudar tipo de forma restritiva ou semântica de coluna | não |
| mudar grão ou chave | não |
| mudar filtro (deixar de incluir registros) | não, mesmo sem mudar schema |

## Regras de decisão

- Fluxo de dependência só vai **para frente**: bronze → silver → gold. Gold não
  lê bronze; silver não lê gold.
- Regra de limpeza usada por mais de um modelo gold → desça para o silver.
- Na dúvida se um dado bruto será necessário depois, mantenha no bronze, não
  no silver.
- Bronze é imutável na prática: correções vão para silver (com regra registrada),
  não por `UPDATE` no bronze.
- Camadas extras (staging, intermediate, platinum) são aceitáveis se o projeto
  as define; siga a convenção existente em vez de impor três camadas.
- Volume: silver e gold incrementais quando a origem cresce; bronze
  particionado por data de ingestão (ver `particionamento`).

## Armadilhas comuns

- Silver que é só cópia do bronze com outro nome (sem tipagem nem dedup).
- Lógica de negócio duplicada em vários modelos gold com pequenas diferenças.
- Gold lendo direto do bronze "porque era mais rápido".
- Dedup no bronze, perdendo a capacidade de auditar e reprocessar.
- PII crua propagada até o gold sem necessidade.
- Mudança "pequena" num silver muito usado quebrando dezenas de gold.
- Tipos inferidos automaticamente no bronze mudando entre arquivos (coluna que
  vira número num dia e texto no outro). Bronze em texto e tipagem explícita no
  silver é mais robusto.

## Como o agente deve agir

1. Identifique a camada do modelo (convenção de pastas, prefixos, schemas,
   tags) antes de propor mudança. Diga em qual camada a mudança deveria morar.
2. Recuse (ou alerte) dependência reversa: gold → bronze, silver → gold.
3. Para mudança em silver/gold, rode impact analysis e classifique a mudança
   como compatível ou quebra, usando a tabela acima.
4. Valide o contrato no resultado: schema (colunas/tipos), chave única, testes
   de não nulo e domínio, contagem de linhas comparada à versão anterior.
5. Coluna nova propagando silver → gold: confira que ela existe e tem o mesmo
   tipo em todo o caminho, e que a contagem de linhas do gold não mudou (coluna
   nova não deveria alterar o grão).
6. Nunca reescreva bronze. Se a correção exige mudar bronze, pare e pergunte.
