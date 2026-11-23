# kafka

Adapter de `StreamPort` para Apache Kafka (entry point `kafka`). Usa o
[`aiokafka`](https://pypi.org/project/aiokafka/) — cliente asyncio nativo, com
wheels para Windows, Linux e macOS — e o `httpx2` para o Schema Registry.
Somente leitura: não produz mensagens, não cria tópicos e não grava offsets.

## Configuração

```yaml
streaming:
  adapter: kafka
  bootstrap_servers: localhost:9092        # ou lista
  # security_protocol: SASL_SSL            # PLAINTEXT (padrão) | SSL | SASL_PLAINTEXT | SASL_SSL
  # sasl_mechanism: SCRAM-SHA-512          # PLAIN (padrão) | SCRAM-SHA-256 | SCRAM-SHA-512
  # sasl_username: env://KAFKA_USER
  # sasl_password: env://KAFKA_PASSWORD
  # schema_registry_url: http://localhost:8081
```

| Chave | Padrão | Descrição |
| --- | --- | --- |
| `bootstrap_servers` | — (obrigatória) | `host:porta`, separados por vírgula ou em lista |
| `security_protocol` | `PLAINTEXT` | `SSL`/`SASL_SSL` usam as CAs do sistema |
| `sasl_mechanism` | `PLAIN` | só com `SASL_*`; exige usuário e senha |
| `sasl_username` / `sasl_password` | — | já resolvidas pelo motor; nunca logadas |
| `schema_registry_url` | — | sem ela, `schema()` devolve `None` |
| `schema_registry_username` / `schema_registry_password` | — | autenticação básica do registry |
| `client_id` | `dengcode` | aparece nos logs/quotas do broker |
| `timeout_s` | `30` | prazo por operação (conexão + metadados) |

## Operações

- `list_topics()`: tópicos não internos (sem `__*`), partições e `retention.ms`
  em horas (`None` = sem limite ou sem permissão de DescribeConfigs).
- `sample(topic, max_messages, timeout_s)`: as **últimas** N mensagens (por
  partição, recorte global pelas mais recentes), lidas por um consumidor sem
  `group_id`, com atribuição manual e sem commit. Itens:
  `{partition, offset, timestamp (ISO 8601 UTC), key (texto), value}`; o valor
  vira JSON quando possível, senão texto UTF-8; binário vira `"base64:..."`.
  Valores acima de 64 KB são cortados. O motor mascara PII antes do LLM.
- `consumer_lag(topic, group)`: `{partição: lag}` = offset final − offset
  confirmado do grupo; sem offset confirmado, o lag é tudo o que está retido.
- `schema(topic)`: `GET /subjects/<topic>-value/versions/latest` no registry
  (subject inexistente → `None`); Avro/JSON Schema voltam como dicionário.

Metadados são pedidos sempre para todos os tópicos: pedir um tópico pelo nome
faria o broker criá-lo quando `auto.create.topics.enable=true`.

## Erros

Tópico inexistente → `NotFoundError`; SASL/ACL → `AuthError`; broker
inalcançável, coordenador indisponível e timeouts → `TransientError` (erros
passageiros do broker têm até 4 tentativas dentro do prazo); o resto →
`AdapterError`. Senhas são removidas das mensagens.

## Testes

```
uv run pytest packages/adapters/kafka -m "not integration"   # rápidos
uv run pytest packages/adapters/kafka -m integration         # Kafka KRaft via testcontainers
```

Stack local: `docker/oss/kafka.yaml` (broker + produtor do tópico `pedidos`).
