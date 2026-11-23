-- Cópia fiel da origem: tudo texto, sem limpeza.
select * from {{ source('raw', 'itens_pedido') }}
