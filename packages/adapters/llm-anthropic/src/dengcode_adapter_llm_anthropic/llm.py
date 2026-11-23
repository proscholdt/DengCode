"""LLMPort para a API de Mensagens da Anthropic (SDK oficial ``anthropic``).

Pontos de atenção:
- o turno do assistente volta *inalterado* (``ProviderState`` com o ``content``
  original): blocos de raciocínio são ligados ao prefixo da conversa;
- quando o core compacta o histórico (edição), pedimos ``drop_block`` para o
  servidor descartar blocos que não batem em vez de recusar a requisição;
- o retry é nosso (``max_retries=0`` no SDK).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from importlib.resources import files
from typing import Any, ClassVar

import anthropic
import yaml

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    TransientError,
)
from dengcode_core.llm.events import (
    ProviderState,
    Stop,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from dengcode_core.llm.messages import (
    ChatRequest,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from dengcode_core.llm.port import estimate_request_tokens
from dengcode_core.llm.profile import ModelProfile, resolve_profile
from dengcode_core.llm.retry import RetryPolicy, parse_retry_after, stream_with_retry
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind

log = logging.getLogger(__name__)

PROVIDER = "anthropic"
BETA_BINDING = "thinking-binding-controls-2026-08-01"
BETA_FALLBACK = "server-side-fallback-2026-07-01"
DEFAULT_MAX_TOKENS = 64_000

_STOP: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "end",
    "pause_turn": "end",
}


def _load_table() -> dict[str, dict[str, Any]]:
    raw = files(__package__).joinpath("model_profiles.yaml").read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    return {k: v for k, v in data.items() if not k.startswith("_")}


class AnthropicLLM:
    info: ClassVar[AdapterInfo] = AdapterInfo(name=PROVIDER, port=PortKind.LLM, version="0.1.0")
    name = PROVIDER
    default_models: ClassVar[dict[str, str]] = {
        "main": "claude-opus-5-5",
        "fast": "claude-haiku-5-5",
    }

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        retry: RetryPolicy | None = None,
        effort: str | None = "high",
        refusal_fallback: bool = True,
        eager_input_streaming: bool = True,
        profile_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()
        self._effort = effort
        self._refusal_fallback = refusal_fallback
        self._eager = eager_input_streaming
        self._overrides = profile_overrides or {}
        self._table = _load_table()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> AnthropicLLM:
        try:
            client = anthropic.AsyncAnthropic(
                api_key=cfg.get("api_key"),
                base_url=cfg.get("base_url"),
                timeout=float(cfg.get("timeout_s", 600)),
                max_retries=0,
            )
        except anthropic.AnthropicError as exc:
            raise AuthError(f"credenciais da Anthropic indisponíveis: {exc}") from exc
        return cls(
            client,
            effort=cfg.get("effort", "high"),
            refusal_fallback=bool(cfg.get("refusal_fallback", True)),
            eager_input_streaming=bool(cfg.get("eager_input_streaming", True)),
            profile_overrides=cfg.get("profiles"),
        )

    # ------------------------------------------------------------------ porta
    def profile(self, model: str) -> ModelProfile:
        table = {
            k: {f: v for f, v in row.items() if f != "features"} for k, row in self._table.items()
        }
        return resolve_profile(PROVIDER, model, table, self._overrides)

    def _features(self, model: str) -> dict[str, bool]:
        row = self._table.get(model)
        if row is None:
            prefixes = sorted((k for k in self._table if model.startswith(k)), key=len)
            row = self._table[prefixes[-1]] if prefixes else {}
        return dict(row.get("features") or {})

    async def count_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request)

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        return stream_with_retry(lambda: self._stream_once(request), self._retry)

    # ------------------------------------------------------------------ tradução
    def build_params(self, request: ChatRequest) -> dict[str, Any]:
        profile = self.profile(request.model)
        feats = self._features(request.model)
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens
            or min(profile.max_output_tokens, DEFAULT_MAX_TOKENS),
            "messages": [m for msg in request.non_system() for m in _to_wire(msg)],
        }
        system = request.system_text()
        if system:
            params["system"] = system
        if request.tools:
            params["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.json_schema,
                    **({"eager_input_streaming": True} if self._eager else {}),
                }
                for t in request.tools
            ]
        if profile.supports_prompt_caching:
            params["cache_control"] = {"type": "ephemeral"}
        betas: list[str] = []
        if feats.get("adaptive_thinking"):
            thinking: dict[str, Any] = {"type": "adaptive"}
            if feats.get("preserved_thinking"):
                thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
                betas.append(BETA_BINDING)
            params["thinking"] = thinking
        if feats.get("effort") and self._effort:
            params["output_config"] = {"effort": self._effort}
        if feats.get("server_fallback") and self._refusal_fallback:
            params["fallbacks"] = "default"
            betas.append(BETA_FALLBACK)
        if betas:
            params["betas"] = betas
        return params

    async def _stream_once(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        params = self.build_params(request)
        current_tool: str | None = None
        try:
            async with self._client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool = block.id
                            yield ToolCallStart(id=block.id, name=block.name)
                        else:
                            current_tool = None
                    elif event.type == "text":
                        yield TextDelta(text=event.text)
                    elif event.type == "input_json" and current_tool is not None:
                        yield ToolCallDelta(
                            id=current_tool, arguments_json_fragment=event.partial_json
                        )
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise _map_error(exc) from exc

        for block in final.content:
            if block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                yield ToolCallEnd(id=block.id, arguments=dict(args))
        if final.stop_reason == "refusal":
            yield TextDelta(text="\n[o modelo recusou esta solicitação]")
        usage = final.usage
        cached = getattr(usage, "cache_read_input_tokens", None) or 0
        created = getattr(usage, "cache_creation_input_tokens", None) or 0
        yield Usage(
            input_tokens=(usage.input_tokens or 0) + cached + created,
            output_tokens=usage.output_tokens or 0,
            cached_input_tokens=cached,
        )
        yield ProviderState(
            provider=PROVIDER,
            data={"content": [b.model_dump(mode="json", exclude_none=True) for b in final.content]},
        )
        yield Stop(reason=_STOP.get(final.stop_reason or "end_turn", "end"))


def _to_wire(msg: Message) -> list[dict[str, Any]]:
    if msg.role == "assistant":
        state = msg.provider_state(PROVIDER)
        if state is not None:
            return [{"role": "assistant", "content": state["content"]}]
        content: list[dict[str, Any]] = []
        for p in msg.parts:
            if isinstance(p, TextPart) and p.text:
                content.append({"type": "text", "text": p.text})
            elif isinstance(p, ToolCallPart):
                content.append(
                    {"type": "tool_use", "id": p.id, "name": p.name, "input": p.arguments}
                )
        return [{"role": "assistant", "content": content}] if content else []
    content = []
    for p in msg.parts:
        if isinstance(p, ToolResultPart):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": p.call_id,
                    "content": p.content,
                    "is_error": p.is_error,
                }
            )
        elif isinstance(p, TextPart):
            content.append({"type": "text", "text": p.text})
        elif isinstance(p, ImagePart):
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": p.media_type, "data": p.data_b64},
                }
            )
    return [{"role": "user", "content": content}] if content else []


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    return parse_retry_after(exc.response.headers.get("retry-after"))


def _map_error(exc: anthropic.AnthropicError) -> Exception:
    """Do mais específico para o mais genérico; nunca deixa o tipo do SDK vazar."""
    msg = str(exc)
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimitError(msg, retry_after_s=_retry_after(exc))
    if isinstance(exc, anthropic.AuthenticationError):
        return AuthError(msg)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return PermissionDeniedError(msg)
    if isinstance(exc, anthropic.NotFoundError):
        return NotFoundError(msg)
    if isinstance(exc, anthropic.APIStatusError):
        should = exc.response.headers.get("x-should-retry")
        retryable = exc.status_code >= 500 or exc.status_code in (408, 409, 529)
        if retryable and should != "false":
            return TransientError(msg, retry_after_s=_retry_after(exc))
        return AdapterError(f"{exc.status_code}: {msg}")
    if isinstance(exc, anthropic.APIConnectionError):  # inclui timeout
        return TransientError(msg)
    if isinstance(exc, anthropic.CredentialsError):
        return AuthError(msg)
    return AdapterError(msg)
