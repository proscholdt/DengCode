#!/bin/sh
# Produtor de exemplo do DengCode (roda DENTRO do container apache/kafka, Linux).
#
# 1. espera o broker responder;
# 2. cria o tópico `pedidos` (3 partições, retenção de 7 dias) se não existir;
# 3. publica ~50 eventos JSON de pedidos SINTÉTICOS (chave = id do cliente),
#    alguns com e-mail, CPF e telefone falsos para exercitar o mascaramento;
# 4. grava o offset 5 em cada partição para o grupo `servico-faturamento`,
#    deixando um consumer group com lag para demonstrar `consumer_lag`.
#
# É idempotente: se o tópico já tem mensagens, não publica de novo.
# Todos os dados são inventados (domínio exemplo.com.br, CPFs aleatórios).
set -eu

BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:19092}"
TOPICO="${KAFKA_TOPICO:-pedidos}"
TOTAL="${KAFKA_EVENTOS:-50}"
GRUPO="${KAFKA_GRUPO_ATRASADO:-servico-faturamento}"
BIN=/opt/kafka/bin

echo "aguardando o broker em ${BOOTSTRAP}..."
tentativas=0
until "${BIN}/kafka-topics.sh" --bootstrap-server "${BOOTSTRAP}" --list >/dev/null 2>&1; do
  tentativas=$((tentativas + 1))
  if [ "${tentativas}" -ge 30 ]; then
    echo "o broker não respondeu a tempo" >&2
    exit 1
  fi
  sleep 2
done

"${BIN}/kafka-topics.sh" --bootstrap-server "${BOOTSTRAP}" --create --if-not-exists \
  --topic "${TOPICO}" --partitions 3 --replication-factor 1 \
  --config retention.ms=604800000

existentes=$("${BIN}/kafka-get-offsets.sh" --bootstrap-server "${BOOTSTRAP}" --topic "${TOPICO}" \
  | awk -F: '{ soma += $3 } END { print soma + 0 }')
if [ "${existentes}" -gt 0 ]; then
  echo "o tópico ${TOPICO} já tem ${existentes} mensagens: nada a publicar"
  exit 0
fi

# gera "chave|json" por linha; srand fixo = mesmos dados a cada execução
gerar_eventos() {
  awk -v total="${TOTAL}" 'BEGIN {
    srand(42)
    split("Ana Souza|Bruno Lima|Carla Mendes|Diego Rocha|Elisa Prado|Fábio Nunes|Gabriela Reis|Heitor Alves|Isabela Castro|João Pereira|Larissa Gomes|Marcos Teixeira", nomes, "|")
    split("ana.souza|bruno.lima|carla.mendes|diego.rocha|elisa.prado|fabio.nunes|gabriela.reis|heitor.alves|isabela.castro|joao.pereira|larissa.gomes|marcos.teixeira", emails, "|")
    split("pago|pago|pago|enviado|pendente|cancelado", status, "|")
    split("app|site|loja", canais, "|")
    split("SKU-CAFE-500G|SKU-CANECA-AZUL|SKU-FILTRO-102|SKU-MOEDOR-MAN|SKU-CHA-MATE", skus, "|")
    for (i = 1; i <= total; i++) {
      c = int(rand() * 12) + 1
      segundos = i * 37
      quando = sprintf("2026-01-15T%02d:%02d:%02dZ", 10 + int(segundos / 3600), int(segundos / 60) % 60, segundos % 60)
      itens = ""
      n = int(rand() * 3) + 1
      for (k = 1; k <= n; k++) {
        item = sprintf("{\"sku\":\"%s\",\"quantidade\":%d}", skus[int(rand() * 5) + 1], int(rand() * 4) + 1)
        itens = (k == 1) ? item : itens "," item
      }
      pii = ""
      if (rand() < 0.7) pii = pii sprintf(",\"email\":\"%s@exemplo.com.br\"", emails[c])
      if (rand() < 0.5) pii = pii sprintf(",\"cpf\":\"%03d.%03d.%03d-%02d\"", int(rand() * 1000), int(rand() * 1000), int(rand() * 1000), int(rand() * 100))
      if (rand() < 0.3) pii = pii sprintf(",\"telefone\":\"+55 11 9%04d-%04d\"", int(rand() * 10000), int(rand() * 10000))
      printf "C%03d|{\"pedido_id\":%d,\"cliente_id\":\"C%03d\",\"cliente_nome\":\"%s\"%s,\"valor_total\":%.2f,\"moeda\":\"BRL\",\"status\":\"%s\",\"canal\":\"%s\",\"itens\":[%s],\"criado_em\":\"%s\"}\n", c, 1000 + i, c, nomes[c], pii, 20 + rand() * 880, status[int(rand() * 6) + 1], canais[int(rand() * 3) + 1], itens, quando
    }
  }'
}

echo "publicando ${TOTAL} eventos em ${TOPICO}..."
gerar_eventos | "${BIN}/kafka-console-producer.sh" --bootstrap-server "${BOOTSTRAP}" \
  --topic "${TOPICO}" \
  --property parse.key=true --property "key.separator=|" \
  --producer-property acks=all --producer-property enable.idempotence=true

# o coordenador de grupos pode demorar a subir no primeiro uso: tenta algumas vezes
echo "criando o grupo ${GRUPO} com lag (offset 5 em cada partição)..."
tentativas=0
until "${BIN}/kafka-consumer-groups.sh" --bootstrap-server "${BOOTSTRAP}" --describe \
  --group "${GRUPO}" 2>/dev/null | grep -q "${TOPICO}"; do
  tentativas=$((tentativas + 1))
  if [ "${tentativas}" -ge 10 ]; then
    echo "não consegui gravar os offsets do grupo ${GRUPO}" >&2
    exit 1
  fi
  "${BIN}/kafka-consumer-groups.sh" --bootstrap-server "${BOOTSTRAP}" --group "${GRUPO}" \
    --topic "${TOPICO}" --reset-offsets --to-offset 5 --execute >/dev/null 2>&1 || sleep 3
done

echo "pronto: ${TOTAL} eventos em ${TOPICO}; grupo ${GRUPO} com lag"
