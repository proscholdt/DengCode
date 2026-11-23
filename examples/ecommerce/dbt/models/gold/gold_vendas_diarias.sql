-- Vendas por dia, sem pedidos cancelados.
select
    data_pedido,
    count(*) as quantidade_pedidos,
    sum(valor_total) as receita
from {{ ref('silver_pedidos') }}
where status <> 'cancelado'
group by data_pedido
