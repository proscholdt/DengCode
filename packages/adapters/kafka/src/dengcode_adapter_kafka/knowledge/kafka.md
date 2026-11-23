# Apache Kafka — guia de inspeção e diagnóstico

Kafka é um log distribuído, particionado e replicado: produtores anexam
mensagens ao fim de cada partição e consumidores leem na ordem, guardando até
onde chegaram (o *offset*). Este guia cobre o necessário para investigar um
tópico ou um consumidor sem alterar nada.

## Tópicos, partições e offsets

- **Tópico** é um nome lógico; **partição** é a unidade de paralelismo, ordem e
  armazenamento. Cada partição é um log independente, com offsets inteiros
  crescentes a partir de 0.
- O **offset inicial** (*log start offset*) sobe quando a retenção apaga
  segmentos antigos; o **offset final** (*log end offset*, LEO) é o próximo
  offset a ser escrito. Mensagens retidas = final − inicial (aproximado em
  tópicos compactados ou com transações, que deixam "buracos" de marcadores).
- Número de partições só aumenta. Aumentar muda o mapeamento chave → partição
  e quebra a ordem por chave para dados novos — trate como mudança de contrato.
- Fator de replicação e `min.insync.replicas` definem durabilidade: com
  `acks=all` e `min.insync.replicas=2`, perder um broker não perde dados.

## Consumer groups e lag

- Um **consumer group** divide as partições entre seus membros: cada partição
  tem no máximo um consumidor ativo por grupo. Mais consumidores que partições
  = consumidores ociosos.
- O grupo grava o **offset confirmado** (*committed offset*) no tópico interno
  `__consumer_offsets`. É o ponto de retomada após reinício ou rebalanceamento.
- **Lag** por partição = offset final − offset confirmado. Lag total é a soma.
  Sem offset gravado, o grupo começa pelo `auto.offset.reset` (`earliest` lê
  tudo o que está retido; `latest` só o que chegar depois).

### Diagnóstico de lag

1. **Lag crescendo em todas as partições**: o consumidor não acompanha a vazão
   (processamento lento, banco de destino lento, poucas instâncias). Compare a
   taxa de produção com a de consumo; escalar só ajuda até o número de partições.
2. **Lag alto em uma partição só**: *hot partition* (chave dominante, ex.: um
   cliente gigante) ou consumidor travado naquela partição (mensagem venenosa
   reprocessada em loop).
3. **Lag estável e alto, sem crescer**: consumidor parado ou grupo sem membros;
   os offsets confirmados não mudam entre duas medições.
4. **Lag "some" de repente**: a retenção apagou dados não consumidos (perda!) ou
   alguém reposicionou o grupo (`--reset-offsets`). Verifique se o offset
   confirmado ficou abaixo do offset inicial.
5. **Rebalanceamentos frequentes**: `max.poll.interval.ms` estourado por lotes
   pesados ou `session.timeout.ms` curto; o grupo para a cada rebalanceamento.

## Retenção e compactação

- `retention.ms` (tempo) e `retention.bytes` (tamanho por partição) apagam
  segmentos inteiros; `-1` = sem limite. Consumidor com lag maior que a
  retenção perde dados silenciosamente.
- `cleanup.policy=compact` mantém só a última mensagem de cada chave (tabelas de
  estado, CDC); mensagem com valor nulo (*tombstone*) apaga a chave depois de
  `delete.retention.ms`. `compact,delete` combina as duas políticas.

## Chaves e ordenação

- A ordem só é garantida **dentro de uma partição**. O particionador padrão faz
  hash da chave: mesma chave → mesma partição → ordem preservada por chave.
- Sem chave, as mensagens são espalhadas (*sticky partitioner*): não há ordem
  entre elas. Escolha a chave pela entidade cuja ordem importa (pedido, cliente).
- Com `max.in.flight.requests.per.connection > 1` e novas tentativas, a ordem
  pode inverter — a menos que o produtor seja idempotente.

## Produtor idempotente e exactly-once

- `enable.idempotence=true` (padrão nos clientes recentes) elimina duplicatas e
  inversões causadas por novas tentativas do **produtor**, por partição e por
  sessão do produtor. Exige `acks=all`.
- **Transações** (`transactional.id`) tornam atômica a escrita em várias
  partições junto com o commit de offsets: é o *exactly-once* de
  consumir-processar-produzir **dentro do Kafka**.
- Ressalvas: consumidores só enxergam o efeito transacional com
  `isolation.level=read_committed`; efeitos externos (gravar no banco, chamar
  API) não entram na transação — use escrita idempotente (upsert por chave,
  deduplicação por id de evento) ou o padrão *outbox*. Na prática, projete para
  *at-least-once* + idempotência no destino.

## Schema Registry e compatibilidade

- O registry guarda versões de schema por **subject**; o padrão é
  `<tópico>-value` (e `<tópico>-key`). Mensagens serializadas com ele começam
  com o byte mágico `0` + id do schema (4 bytes) — por isso parecem binárias.
- Modos de compatibilidade: `BACKWARD` (padrão; consumidores novos leem dados
  antigos — pode remover campos e adicionar campos com default), `FORWARD`
  (consumidores antigos leem dados novos), `FULL` (ambos) e as variantes
  `_TRANSITIVE` (contra todas as versões, não só a anterior). `NONE` é perigoso.
- Antes de mudar um schema: confira o modo do subject, prefira adicionar campos
  opcionais com default e nunca reutilize o nome de um campo com outro tipo.

## Inspeção segura (somente leitura)

- Para amostrar, use um consumidor **sem grupo** e com atribuição manual de
  partições, `enable.auto.commit=false` e **nunca** faça commit: entrar com o
  `group.id` de produção roubaria partições do consumidor real e mexeria nos
  offsets dele.
- Leia as últimas N mensagens posicionando em `fim − N` por partição em vez de
  ler desde o começo; limite tempo e quantidade.
- Peça metadados de todos os tópicos de uma vez: consultar um tópico inexistente
  pelo nome pode **criá-lo** quando `auto.create.topics.enable=true`.
- Lag se mede comparando offsets confirmados (OffsetFetch) com offsets finais —
  não é preciso entrar no grupo.
- O conteúdo das mensagens pode ter PII (e-mail, CPF, telefone): mostre amostras
  pequenas e mascaradas; nunca copie payloads crus para logs ou tickets.
- Ações de escrita (`--reset-offsets`, apagar tópico, alterar config, produzir
  mensagens) exigem aprovação explícita e plano de reversão.
