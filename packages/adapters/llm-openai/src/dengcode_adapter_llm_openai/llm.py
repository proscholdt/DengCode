"""LLMPort para a API Chat Completions (SDK oficial ``openai``).

Atende OpenAI, Azure OpenAI (``variant: azure``) e servidores compatíveis
(``base_url``). O retry é nosso (``max_retries=0`` no SDK).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, ClassVar

import openai
import yaml

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    TransientError,
)
from dengcode_core.llm.events import (
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
from dengcode_core.tools.base import INVALID_JSON_KEY

log = logging.getLogger(__name__)

_FINISH: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end",
}


def _load_table() -> dict[str, dict[str, Any]]:
    raw = files(__package__).joinpath("model_profiles.yaml").read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    return {k: v for k, v in data.items() if not k.startswith("_")}


@dataclass
class _PendingCall:
    id: str
    name: str
    fragments: list[str] = field(default_factory=lambda: list[str]())


class OpenAILLM:
    info: ClassVar[AdapterInfo] = AdapterInfo(name="openai", port=PortKind.LLM, version="0.1.0")
    name = "openai"

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        provider: str = "openai",
        retry: RetryPolicy | None = None,
        profile_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._provider = provider
        self._retry = retry or RetryPolicy()
        self._overrides = profile_overrides or {}
        self._table = _load_table()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> OpenAILLM:
        variant = cfg.get("variant")
        timeout = float(cfg.get("timeout_s", 600))
        try:
            if variant == "azure":
                endpoint = cfg.get("endpoint") or cfg.get("azure_endpoint")
                if not endpoint:
                    raise ConfigError("Azure OpenAI exige 'endpoint' na stack")
                client: openai.AsyncOpenAI = openai.AsyncAzureOpenAI(
                    azure_endpoint=endpoint,
                    api_version=cfg.get("api_version", "2024-10-21"),
                    api_key=cfg.get("api_key"),
                    timeout=timeout,
                    max_retries=0,
                )
            else:
                client = openai.AsyncOpenAI(
                    api_key=cfg.get("api_key"),
                    base_url=cfg.get("base_url"),
                    timeout=timeout,
                    max_retries=0,
                )
        except openai.OpenAIError as exc:
            raise AuthError(f"credenciais do provedor indisponíveis: {exc}") from exc
        provider = "azure_openai" if variant == "azure" else "openai"
        return cls(client, provider=provider, profile_overrides=cfg.get("profiles"))

    # ------------------------------------------------------------------ porta
    def profile(self, model: str) -> ModelProfile:
        return resolve_profile(self._provider, model, self._table, self._overrides)

    async def count_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request)

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        return stream_with_retry(lambda: self._stream_once(request), self._retry)

    def build_params(self, request: ChatRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "messages": [m for msg in request.messages for m in to_wire(msg)],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.json_schema,
                    },
                }
                for t in request.tools
            ]
        if request.max_output_tokens:
            params["max_completion_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            params["temperature"] = request.temperature
        return params

    async def _stream_once(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        pending: dict[int, _PendingCall] = {}
        finish = "stop"
        usage: Usage | None = None
        try:
            stream = await self._client.chat.completions.create(**self.build_params(request))
            async for chunk in stream:
                if chunk.usage is not None:
                    cached = 0
                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    if details is not None:
                        cached = getattr(details, "cached_tokens", 0) or 0
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                        cached_input_tokens=cached,
                    )
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta.content:
                        yield TextDelta(text=delta.content)
                    for tc in delta.tool_calls or []:
                        call = pending.get(tc.index)
                        if call is None:
                            call_id = tc.id or f"call_{tc.index}"
                            name = tc.function.name if tc.function and tc.function.name else "?"
                            call = pending[tc.index] = _PendingCall(call_id, name)
                            yield ToolCallStart(id=call_id, name=name)
                        if tc.function and tc.function.arguments:
                            call.fragments.append(tc.function.arguments)
                            yield ToolCallDelta(
                                id=call.id, arguments_json_fragment=tc.function.arguments
                            )
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except openai.OpenAIError as exc:
            raise map_error(exc) from exc

        for index in sorted(pending):
            call = pending[index]
            yield ToolCallEnd(id=call.id, arguments=_parse_args("".join(call.fragments)))
        if usage is not None:
            yield Usage(**usage.model_dump(exclude={"type"}))
        reason = _FINISH.get(finish, "end")
        if pending and reason == "end":
            reason = "tool_use"
        yield Stop(reason=reason)


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # argumentos inválidos viram erro de validação no core (não executa nada)
        return {INVALID_JSON_KEY: raw[:500]}
    return value if isinstance(value, dict) else {INVALID_JSON_KEY: raw[:500]}


def to_wire(msg: Message) -> list[dict[str, Any]]:
    if msg.role == "system":
        return [{"role": "system", "content": msg.text_content()}]
    if msg.role == "assistant":
        out: dict[str, Any] = {"role": "assistant", "content": msg.text_content() or None}
        calls = [p for p in msg.parts if isinstance(p, ToolCallPart)]
        if calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.arguments, ensure_ascii=False),
                    },
                }
                for c in calls
            ]
        return [out]
    wire: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": p.call_id, "content": p.content}
        for p in msg.parts
        if isinstance(p, ToolResultPart)
    ]
    content: list[dict[str, Any]] = []
    for p in msg.parts:
        if isinstance(p, TextPart):
            content.append({"type": "text", "text": p.text})
        elif isinstance(p, ImagePart):
            url = f"data:{p.media_type};base64,{p.data_b64}"
            content.append({"type": "image_url", "image_url": {"url": url}})
    if content:
        only_text = all(c["type"] == "text" for c in content)
        text = "\n".join(c["text"] for c in content) if only_text else content
        wire.append({"role": "user", "content": text})
    return wire


def map_error(exc: openai.OpenAIError) -> Exception:
    msg = str(exc)
    if isinstance(exc, openai.RateLimitError):
        return RateLimitError(
            msg, retry_after_s=parse_retry_after(exc.response.headers.get("retry-after"))
        )
    if isinstance(exc, openai.AuthenticationError):
        return AuthError(msg)
    if isinstance(exc, openai.PermissionDeniedError):
        return PermissionDeniedError(msg)
    if isinstance(exc, openai.NotFoundError):
        return NotFoundError(msg)
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409):
            return TransientError(
                msg, retry_after_s=parse_retry_after(exc.response.headers.get("retry-after"))
            )
        return AdapterError(f"{exc.status_code}: {msg}")
    if isinstance(exc, openai.APIConnectionError):
        return TransientError(msg)
    return AdapterError(msg)
