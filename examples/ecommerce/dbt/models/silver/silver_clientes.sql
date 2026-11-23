-- Clientes tipados. As colunas de PII ficam como estão: o mascaramento
-- acontece no DengCode antes de qualquer dado chegar ao LLM.
select
    cast(trim(id_cliente) as integer) as id_cliente,
    trim(nome) as nome,
    lower(trim(email)) as email,
    trim(cpf) as cpf,
    trim(telefone) as telefone,
    trim(cidade) as cidade,
    upper(trim(uf)) as uf,
    cast(trim(data_cadastro) as date) as data_cadastro
from {{ ref('bronze_clientes') }}
