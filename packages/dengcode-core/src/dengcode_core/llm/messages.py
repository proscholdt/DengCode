"""Formato canônico de mensagens e ferramentas (ver llm-formato-canonico.md)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    media_type: str
    data_b64: str


class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False


Part = Annotated[TextPart | ImagePart | ToolCallPart | ToolResultPart, Field(discriminator="type")]


class Message(BaseModel):
    role: Role
    parts: list[Part]

    @classmethod
    def text(cls, role: Role, text: str) -> Message:
        return cls(role=role, parts=[TextPart(text=text)])

    def text_content(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.parts if isinstance(p, ToolCallPart)]

    def tool_results(self) -> list[ToolResultPart]:
        return [p for p in self.parts if isinstance(p, ToolResultPart)]


class ToolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    description: str
    json_schema: dict[str, Any]


class ChatRequest(BaseModel):
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=lambda: list[ToolSpec]())
    max_output_tokens: int | None = None
    temperature: float | None = None
    cache_breakpoints: list[int] = Field(default_factory=lambda: list[int]())
    metadata: dict[str, str] = Field(default_factory=lambda: dict[str, str]())

    def system_text(self) -> str:
        return "\n\n".join(m.text_content() for m in self.messages if m.role == "system")

    def non_system(self) -> list[Message]:
        return [m for m in self.messages if m.role != "system"]
