# Especificação — Stacks e configuração

## Onde ficam

| Origem | Caminho |
| --- | --- |
| Presets embutidos | `stacks/*.yaml` (empacotados no motor) |
| Do usuário | `~/.dengcode/stacks/*.yaml` |
| Do projeto | `<repo>/.dengcode/stacks/*.yaml` |

Resolução por nome: projeto > usuário > embutido.

## Schema (pydantic)

```yaml
name: string                    # obrigatório, único
extends: string | null          # nome de outra stack
description: string | null

llm:                            # ver llm-formato-canonico.md
  provider: string
  model: string                 # atalho; ou "models: {main, fast}"
  fallback: {...} | null
embedding: { provider: string, model: string } | null

storage:        AdapterRef | null
table_format:   delta | iceberg | hudi | parquet | null
compute:        list[AdapterRef]          # o primeiro é o padrão
orchestrator:   AdapterRef | null
catalog:        AdapterRef | null
transform:      AdapterRef | null
streaming:      AdapterRef | null
stream_compute: list[AdapterRef]
secrets:        AdapterRef | null
memory:         AdapterRef                # padrão: { adapter: sqlite }

environments:                   # opcional; padrão: só "dev"
  <nome>:
    credentials_profile: string | null
    permission_mode: ask | auto | read_only
    read_only: bool
    cost_limit: { usd: float | null, bytes: int | null }
    overrides: { <porta>: {...} }   # ex.: outro workspace Databricks em prod

policy:
  protected_tables: list[str]
  shell_allowlist: list[str]
```

`AdapterRef`:

```yaml
{ adapter: string, variant: string | null, <chaves livres do adapter>... }
```

## Herança (`extends`)

- Merge profundo de dicionários; listas são **substituídas**, não concatenadas.
- Ciclo de `extends` → erro.
- `null` explícito remove a porta herdada.
- Seção `llm`: trocar de `provider` recomeça a seção (chaves de um adapter não
  valem para outro); `model` e `models` substituem um ao outro.

## Validação

`dengcode stack validate <nome>`:
1. Schema pydantic.
2. Cada `adapter` existe nos entry points instalados (senão: mensagem
   dizendo qual extra instalar, ex.: `uv tool install "dengcode[aws]"`).
3. Cada adapter valida suas chaves (`AdapterConfig` próprio).

`dengcode stack doctor <nome> [--env dev]`: conecta em cada porta e executa uma
operação inofensiva (listar, `SELECT 1`, listar pipelines). Saída em tabela
✓/✗ com a causa.

## Credenciais

Nunca no YAML. `credentials_profile` aponta para:
- perfil nativo da nuvem (`az login`, `aws sso`, `gcloud auth application-default`), ou
- chaves no `SecretsPort` (`secret://key-vault/nome`), ou
- variáveis de ambiente / `.env` (`env://ANTHROPIC_API_KEY`).
