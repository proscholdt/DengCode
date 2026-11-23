"""LLMPort para o Ollama local (HTTP ``/api/chat``, NDJSON em streaming)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from importlib.resources import files
from typing import Any, ClassVar

import httpx2
import yaml

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    TransientError,
)
from dengcode_core.llm.events import (
    Stop,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from dengcode_core.llm.messages import ChatRequest, ImagePart, Message, ToolCallPart, ToolResultPart
from dengcode_core.llm.port import estimate_request_tokens
from dengcode_core.llm.profile import ModelProfile, resolve_profile
from dengcode_core.llm.retry import RetryPolicy, parse_retry_after, stream_with_retry
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind

log = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"


def _load_table() -> dict[str, dict[str, Any]]:
    raw = files(__package__).joinpath("model_profiles.yaml").read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    return {k: v for k, v in data.items() if not k.startswith("_")}


class OllamaLLM:
    info: ClassVar[AdapterInfo] = AdapterInfo(name="ollama", port=PortKind.LLM, version="0.1.0")
    name = "ollama"

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        retry: RetryPolicy | None = None,
        num_ctx: int = 32_768,
        profile_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy(max_attempts=3)
        self._num_ctx = num_ctx
        self._overrides = profile_overrides or {}
        self._table = _load_table()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> OllamaLLM:
        host = str(cfg.get("host") or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST)
        if not host.startswith("http"):
            host = f"http://{host}"
        client = httpx2.AsyncClient(base_url=host, timeout=float(cfg.get("timeout_s", 600)))
        return cls(
            client, num_ctx=int(cfg.get("num_ctx", 32_768)), profile_overrides=cfg.get("profiles")
        )

    def profile(self, model: str) -> ModelProfile:
        base = model.split(":", 1)[0]
        profile = resolve_profile("ollama", base, self._table, self._overrides)
        window = min(profile.context_window, self._num_ctx)
        update: dict[str, Any] = {"model": model, "context_window": window}
        if model in self._overrides:
            update.update(self._overrides[model])
        return profile.model_copy(update=update)

    async def count_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request)

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        return stream_with_retry(lambda: self._stream_once(request), self._retry)

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        profile = self.profile(request.model)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": to_wire(request.messages),
            "stream": True,
            "options": {"num_ctx": profile.context_window},
        }
        if request.tools:
            payload["tools"] = [
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
            payload["options"]["num_predict"] = request.max_output_tokens
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        return payload

    async def _stream_once(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        payload = self.build_payload(request)
        calls: list[tuple[str, str, dict[str, Any]]] = []
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise _status_error(resp.status_code, body, resp.headers, request.model)
                done: dict[str, Any] = {}
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk: dict[str, Any] = json.loads(line)
                    if chunk.get("error"):
                        raise AdapterError(f"ollama: {chunk['error']}")
                    message: dict[str, Any] = chunk.get("message") or {}
                    if message.get("content"):
                        yield TextDelta(text=str(message["content"]))
                    for tc in message.get("tool_calls") or []:
                        fn: dict[str, Any] = tc.get("function") or {}
                        call_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")
                        name = str(fn.get("name", "?"))
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            args = json.loads(args) if args.strip() else {}
                        yield ToolCallStart(id=call_id, name=name)
                        yield ToolCallDelta(id=call_id, arguments_json_fragment=json.dumps(args))
                        calls.append((call_id, name, args))
                    if chunk.get("done"):
                        done = chunk
        except httpx2.ConnectError as exc:
            raise AdapterError(
                f"não foi possível conectar ao Ollama em {self._client.base_url}; "
                "ele está rodando? (ollama serve)"
            ) from exc
        except (httpx2.TimeoutException, httpx2.RemoteProtocolError, httpx2.ReadError) as exc:
            raise TransientError(f"ollama: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AdapterError(f"resposta inválida do Ollama: {exc}") from exc

        for call_id, _, args in calls:
            yield ToolCallEnd(id=call_id, arguments=args)
        yield Usage(
            input_tokens=int(done.get("prompt_eval_count", 0)),
            output_tokens=int(done.get("eval_count", 0)),
        )
        reason = done.get("done_reason", "stop")
        if calls:
            yield Stop(reason="tool_use")
        else:
            yield Stop(reason="max_tokens" if reason == "length" else "end")


def _status_error(status: int, body: str, headers: Mapping[str, str], model: str) -> Exception:
    if status in (401, 403):
        return AuthError(f"ollama {status}: o proxy na frente do Ollama recusou a credencial")
    if status == 404:
        return NotFoundError(f"modelo '{model}' não encontrado no Ollama (ollama pull {model})")
    if "does not support tools" in body:
        return ConfigError(f"o modelo '{model}' não suporta ferramentas no Ollama; escolha outro")
    if status == 429 or status >= 500:
        return TransientError(
            f"ollama {status}: {body[:300]}",
            retry_after_s=parse_retry_after(headers.get("retry-after")),
        )
    return AdapterError(f"ollama {status}: {body[:300]}")


def to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    wire: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": msg.text_content()}
            calls = [p for p in msg.parts if isinstance(p, ToolCallPart)]
            for c in calls:
                names[c.id] = c.name
            if calls:
                out["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}} for c in calls
                ]
            wire.append(out)
            continue
        for p in msg.parts:
            if isinstance(p, ToolResultPart):
                wire.append(
                    {"role": "tool", "content": p.content, "tool_name": names.get(p.call_id, "")}
                )
        text = msg.text_content()
        images = [p.data_b64 for p in msg.parts if isinstance(p, ImagePart)]
        if text or images:
            item: dict[str, Any] = {"role": msg.role, "content": text}
            if images:
                item["images"] = images
            wire.append(item)
    return wire
