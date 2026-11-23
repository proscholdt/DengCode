"""Gera os CSVs sintéticos do exemplo de e-commerce (determinístico).

Uso: ``uv run python examples/ecommerce/gerar_dados.py``

Dados 100% fictícios: CPFs com dígitos verificadores válidos mas inventados,
e-mails em domínios de exemplo. O arquivo de pedidos é propositalmente "sujo"
(duplicatas, status com caixa misturada, valores como texto, datas em dois
formatos) para exercitar a camada silver.
"""

from __future__ import annotations

import csv
import random
import unicodedata
from datetime import date, timedelta
from pathlib import Path

SEED = 20261123
OUT = Path(__file__).resolve().parent

NOMES = [
    "Ana",
    "Bruno",
    "Carla",
    "Diego",
    "Eduarda",
    "Felipe",
    "Gabriela",
    "Henrique",
    "Isabela",
    "João",
    "Larissa",
    "Marcos",
    "Natália",
    "Otávio",
    "Patrícia",
    "Rafael",
    "Sabrina",
    "Thiago",
    "Vanessa",
    "Wagner",
]
SOBRENOMES = [
    "Silva",
    "Souza",
    "Oliveira",
    "Santos",
    "Pereira",
    "Lima",
    "Carvalho",
    "Ferreira",
    "Rodrigues",
    "Almeida",
    "Costa",
    "Gomes",
    "Ribeiro",
    "Martins",
    "Araújo",
]
CIDADES = [
    ("São Paulo", "SP"),
    ("Rio de Janeiro", "RJ"),
    ("Belo Horizonte", "MG"),
    ("Curitiba", "PR"),
    ("Porto Alegre", "RS"),
    ("Recife", "PE"),
    ("Salvador", "BA"),
    ("Fortaleza", "CE"),
    ("Goiânia", "GO"),
    ("Manaus", "AM"),
]
DOMINIOS = ["exemplo.com", "exemplo.com.br", "teste.org"]
PRODUTOS = [
    ("Fone Bluetooth", 199.90),
    ("Teclado Mecânico", 349.00),
    ("Mouse sem fio", 89.90),
    ("Monitor 27", 1299.00),
    ("Cadeira Gamer", 999.90),
    ("Webcam HD", 249.50),
    ("SSD 1TB", 459.00),
    ("Hub USB-C", 129.90),
    ("Mochila Notebook", 179.00),
    ("Luminária LED", 99.90),
]
STATUS = ["entregue", "ENTREGUE", "Entregue", "enviado", "cancelado", "pendente"]
CANAIS = ["site", "app", "loja", "marketplace"]


def _slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return plain.lower().replace(" ", ".")


def _cpf(rng: random.Random) -> str:
    digits = [rng.randint(0, 9) for _ in range(9)]
    for size in (9, 10):
        total = sum(d * (size + 1 - i) for i, d in enumerate(digits[:size]))
        digits.append((total * 10) % 11 % 10)
    s = "".join(map(str, digits))
    return f"{s[:3]}.{s[3:6]}.{s[6:9]}-{s[9:]}"


def _write(name: str, header: list[str], rows: list[list[object]]) -> None:
    with (OUT / name).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    rng = random.Random(SEED)
    clientes: list[list[object]] = []
    for cid in range(1, 61):
        nome = f"{rng.choice(NOMES)} {rng.choice(SOBRENOMES)}"
        cidade, uf = rng.choice(CIDADES)
        email = f"{_slug(nome)}{cid}@{rng.choice(DOMINIOS)}"
        telefone = f"({rng.randint(11, 99)}) 9{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}"
        cadastro = date(2024, 1, 1) + timedelta(days=rng.randint(0, 600))
        clientes.append([cid, nome, email, _cpf(rng), telefone, cidade, uf, cadastro.isoformat()])
    _write(
        "clientes.csv",
        ["id_cliente", "nome", "email", "cpf", "telefone", "cidade", "uf", "data_cadastro"],
        clientes,
    )

    pedidos: list[list[object]] = []
    itens: list[list[object]] = []
    for pid in range(1, 301):
        dia = date(2026, 9, 1) + timedelta(days=rng.randint(0, 80))
        data_txt = dia.isoformat() if rng.random() < 0.8 else dia.strftime("%d/%m/%Y")
        total = 0.0
        for _ in range(rng.randint(1, 4)):
            produto, preco = rng.choice(PRODUTOS)
            qtd = rng.randint(1, 3)
            total += preco * qtd
            itens.append([pid, produto, qtd, f"{preco:.2f}"])
        valor = f"{total:.2f}" if rng.random() < 0.9 else f" {total:.2f} "
        row: list[object] = [
            pid,
            rng.randint(1, 60),
            data_txt,
            rng.choice(STATUS),
            valor,
            rng.choice(CANAIS),
        ]
        pedidos.append(row)
        if rng.random() < 0.06:  # duplicata exata vinda da origem
            pedidos.append(list(row))
    _write(
        "pedidos.csv",
        ["id_pedido", "id_cliente", "data_pedido", "status", "valor_total", "canal"],
        pedidos,
    )
    _write("itens_pedido.csv", ["id_pedido", "produto", "quantidade", "preco_unitario"], itens)


if __name__ == "__main__":
    main()
