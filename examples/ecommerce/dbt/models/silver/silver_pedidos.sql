-- Pedidos limpos: uma linha por pedido, tipos corretos.
with tipado as (
    select
        cast(trim(id_pedido) as integer) as id_pedido,
        cast(trim(id_cliente) as integer) as id_cliente,
        -- a origem mistura AAAA-MM-DD e DD/MM/AAAA
        cast(
            coalesce(
                try_strptime(trim(data_pedido), '%Y-%m-%d'),
                try_strptime(trim(data_pedido), '%d/%m/%Y')
            ) as date
        ) as data_pedido,
        lower(trim(status)) as status,
        cast(trim(valor_total) as decimal(12, 2)) as valor_total
    from {{ ref('bronze_pedidos') }}
)

select *
from tipado
-- a origem repete linhas inteiras: fica uma por pedido
qualify row_number() over (partition by id_pedido order by data_pedido) = 1
