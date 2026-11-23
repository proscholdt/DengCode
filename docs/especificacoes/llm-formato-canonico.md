# Especificação — LLM: formato canônico

O core só conhece estes tipos (`dengcode_core.llm`). Cada adapter de provedor
traduz de/para eles. Nenhum tipo de SDK de provedor atravessa a porta.

## Mensagens

```python
class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    media_type: str  # "image/png"
    data_b64: str


class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str  # id canônico; o adapter mapeia ids do provedor
    name: str
    arguments: dict[str, Any]


class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str  # já resumido/truncado pelo core
    is_error: bool = False


Part = Annotated[TextPart | ImagePart | ToolCallPart | ToolResultPart, Field(discriminator="type")]


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    parts: list[Part]
```

Regras de tradução que os adapters precisam tratar:
- Anthropic: `system` vira parâmetro separado; `tool` vira `user` com blocos `tool_result`.
- OpenAI: `tool_calls` na mensagem `assistant`; cada resultado é uma mensagem `role="tool"` com `tool_call_id`.
- Ollama: segue o formato de tools compatível com OpenAI; quando o modelo não suportar tools, o adapter declara `supports_tools=False` e o core recusa a sessão com erro claro.

## Ferramentas

```python
class ToolSpec(BaseModel):
    name: str  # ex.: "query_run" (use [a-z0-9_]; alguns provedores recusam ponto)
    description: str
    json_schema: dict[str, Any]  # JSON Schema do objeto de argumentos
```

## Requisição

```python
class ChatRequest(BaseModel):
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = []
    max_output_tokens: int | None = None
    temperature: float | None = None
    cache_breakpoints: list[
        int
    ] = []  # índices de mensagens onde o provedor pode cachear (se suportar)
    metadata: dict[str, str] = {}
```

## Eventos de streaming

```python
class TextDelta(BaseModel):
    type: Literal["text_delta"]
    text: str


class ToolCallStart(BaseModel):
    type: Literal["tool_call_start"]
    id: str
    name: str


class ToolCallDelta(BaseModel):
    type: Literal["tool_call_delta"]
    id: str
    arguments_json_fragment: str


class ToolCallEnd(BaseModel):
    type: Literal["tool_call_end"]
    id: str
    arguments: dict[str, Any]


class Usage(BaseModel):
    type: Literal["usage"]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


class Stop(BaseModel):
    type: Literal["stop"]
    reason: Literal["end", "tool_use", "max_tokens", "error", "cancelled"]


StreamEvent = TextDelta | ToolCallStart | ToolCallDelta | ToolCallEnd | Usage | Stop
```

O adapter é responsável por montar o JSON dos argumentos a partir dos
fragmentos e emitir `ToolCallEnd` com o dicionário já validado como JSON.

## ModelProfile (flags do modelo)

```python
class ModelProfile(BaseModel):
    provider: str
    model: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_parallel_tools: bool
    supports_prompt_caching: bool
    supports_vision: bool
    supports_extended_thinking: bool
    price_input_per_mtok_usd: float | None
    price_output_per_mtok_usd: float | None
```

Os perfis vêm de uma tabela embutida (`model_profiles.yaml` em cada adapter)
e podem ser sobrescritos na stack. Modelo desconhecido → perfil conservador
(contexto 32k, sem tools paralelas) + aviso.

## Papéis de modelo

Na stack:

```yaml
llm:
  provider: anthropic
  models:
    main: "<modelo principal>"
    fast: "<modelo barato>"       # resumos, compactação, classificação
  fallback: { provider: openai, models: { main: "<modelo>" } }
embedding: { provider: local, model: "<modelo de embedding>" }
```

Atalho aceito: `llm: { provider: x, model: y }` = `main` e `fast` iguais.

## Retry, backoff e fallback

- **Retry** só em erros temporários: 429, 5xx, timeout, conexão.
  Nunca em 400/401/403/404.
- **Backoff** exponencial com jitter: base 1 s, fator 2, teto 30 s, máx. 5 tentativas.
  Respeitar `Retry-After` quando vier.
- **Fallback**: esgotadas as tentativas, se a stack definir `fallback`, a
  sessão troca de provedor e emite um evento `ProviderFallback` para a interface.
- Tudo isso fica dentro do adapter/camada LLM; o loop só vê sucesso ou erro final.

## Provedores

| Fase | Provedores |
| --- | --- |
| 2 | Anthropic, OpenAI (inclui Azure OpenAI via base URL/deployment), Ollama |
| 7 | Bedrock, Vertex AI, Gemini |
