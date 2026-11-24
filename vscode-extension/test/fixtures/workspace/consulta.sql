-- Pedidos por canal de venda
SELECT canal_venda, count(*) AS pedidos
FROM silver.pedidos
GROUP BY canal_venda;
