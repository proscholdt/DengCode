"""Eventos de streaming que todo adapter de LLM emite."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

StopReason = Literal["end", "tool_use", "max_tokens", "error", "cancelled"]


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallStart(BaseModel):
    type: Literal["tool_call_start"] = "tool_call_start"
    id: str
    name: str


class ToolCallDelta(BaseModel):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    id: str
    arguments_json_fragment: str


class ToolCallEnd(BaseModel):
    type: Literal["tool_call_end"] = "tool_call_end"
    id: str
    arguments: dict[str, Any]


class Usage(BaseModel):
    type: Literal["usage"] = "usage"
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


class Stop(BaseModel):
    type: Literal["stop"] = "stop"
    reason: StopReason


StreamEvent = Annotated[
    TextDelta | ToolCallStart | ToolCallDelta | ToolCallEnd | Usage | Stop,
    Field(discriminator="type"),
]
