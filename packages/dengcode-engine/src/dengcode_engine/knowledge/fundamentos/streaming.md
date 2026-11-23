# Streaming: garantias, tempo e estado

## Quando usar / por que importa

Pipelines de streaming processam eventos continuamente e mantêm estado entre eles.
Os erros mais caros aparecem aqui: duplicatas silenciosas, eventos perdidos em
reinícios, agregações que "fecham" cedo demais e atrasos que crescem sem alarme.
Antes de mudar ou diagnosticar um job, entenda a garantia de entrega, a noção de
tempo usada e onde o estado é salvo.

## Garantias de entrega

- **At-most-once**: confirma antes de processar; falhas perdem eventos. Raro em dados.
- **At-least-once**: processa e depois confirma; falhas reprocessam. É o padrão
  realista — o destino precisa ser **idempotente** (upsert por chave, deduplicação
  por id do evento).
- **Exactly-once**: na prática é "efetivamente uma vez" de ponta a ponta e exige
  as três pontas: leitura com posição (offset) controlada, estado com checkpoint e
  escrita transacional ou idempotente atrelada ao mesmo checkpoint. Se o destino
  não participa, o que se tem é at-least-once.

Regra: assuma at-least-once e faça o destino idempotente; trate "exactly-once"
como propriedade a provar, não a supor.

## Tempo de evento x tempo de processamento

- **Tempo de evento**: quando aconteceu (campo do evento). Use para agregações de
  negócio.
- **Tempo de processamento**: quando o job viu o evento. Simples, mas resultados
  mudam com atraso e reprocessamento.
- **Watermark**: estimativa de "até que tempo de evento já vi tudo". Uma janela só
  fecha quando o watermark passa do fim dela. Watermark agressivo descarta mais
  eventos atrasados; conservador atrasa resultados e aumenta estado.

## Janelas

- **Tumbling** (fixas, sem sobreposição): totais por minuto/hora.
- **Sliding/hopping** (sobrepostas): médias móveis.
- **Session** (por inatividade): sessões de usuário; estado cresce com usuários ativos.
- Documente o grão: "janela de 5 min por tempo de evento, atraso permitido 10 min".

## Dados atrasados (late data)

- Defina **atraso permitido** e o que acontece depois: descartar, mandar para uma
  saída lateral (side output) ou reabrir/corrigir agregados.
- Agregados corrigidos exigem destino que aceite atualização (upsert), nunca
  apenas append.

## Estado e checkpoint

- O checkpoint salva posições de leitura + estado das operações. Reinício volta ao
  último checkpoint e reprocessa dali (por isso o destino idempotente).
- Mudar a topologia (operadores, chaves, schema do estado) pode invalidar o
  checkpoint: planeje migração de estado ou reprocessamento a partir da origem.
- Estado grande (joins sem janela, sessões longas) aumenta tempo de checkpoint e
  de recuperação; prefira TTL de estado.

## Particionamento e ordem

- Ordem só é garantida **dentro da partição**. Eventos que precisam de ordem
  (mesma entidade) devem ter a mesma chave.
- Partições "quentes" (chave dominante) limitam o paralelismo e criam lag numa
  partição só.

## Diagnóstico: lag e atraso

- **Lag do consumidor** (mensagens não lidas por partição) crescendo = o job não
  acompanha a entrada. Verifique: lag concentrado em uma partição (chave quente),
  reinícios frequentes (logs), checkpoints lentos ou falhando, destino lento.
- **Atraso de tempo de evento** (agora − watermark) mostra o quão "velhos" estão
  os resultados.
- Picos após deploy costumam ser reprocessamento a partir do último checkpoint.

## Schema e evolução

- Use registro de schema com regra de compatibilidade (normalmente *backward*):
  adicionar campos opcionais é seguro; remover ou mudar tipo quebra consumidores.
- Consumidores devem tolerar campos desconhecidos.

## Como o agente deve agir

1. Leia antes de mudar: tópicos/partições, lag por grupo, status e logs do job,
   amostra **mascarada** de mensagens (nunca confirme offsets ao inspecionar).
2. Ao diagnosticar lag, mostre a distribuição por partição antes de concluir.
3. Ao propor mudança de chave, janela ou schema, explique o impacto no estado e no
   checkpoint e se exige reprocessamento.
4. Verifique idempotência do destino (chave natural/id do evento) antes de
   recomendar reprocessar ou reiniciar a partir de um offset anterior.
5. Registre o incidente na memória (sintoma, causa, solução) ao terminar.
