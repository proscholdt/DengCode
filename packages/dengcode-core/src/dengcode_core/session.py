"""Sessão: histórico, metadados e persistência em JSONL.

Arquivo: ``<sessions_dir>/<id>.jsonl`` — 1ª linha ``{"meta": ...}``, depois uma
mensagem por linha. O diretório é injetado pelo motor
(``~/.dengcode/workspaces/<ws>/sessions/``).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dengcode_core.errors import NotFoundError
from dengcode_core.llm.messages import Message


def _now() -> datetime:
    return datetime.now(UTC)


class SessionMeta(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace: str
    project_dir: str
    stack: str | None = None
    env: str = "dev"
    title: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    system_prompt: str | None = None  # congelado na criação: o prefixo não muda ao retomar


class Session:
    def __init__(self, meta: SessionMeta, messages: list[Message] | None = None) -> None:
        self.meta = meta
        self.messages: list[Message] = list(messages or [])

    @property
    def id(self) -> str:
        return self.meta.id

    def append(self, message: Message) -> None:
        self.messages.append(message)
        self.meta.updated_at = _now()
        if self.meta.title is None and message.role == "user":
            text = message.text_content().strip().splitlines()
            if text:
                self.meta.title = text[0][:80]


class SessionStore:
    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir

    def path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"

    def save(self, session: Session) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self.path(session.id)
        tmp = target.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                json.dumps({"meta": session.meta.model_dump(mode="json")}, ensure_ascii=False)
                + "\n"
            )
            for msg in session.messages:
                fh.write(msg.model_dump_json() + "\n")
        tmp.replace(target)
        return target

    def load(self, session_id: str) -> Session:
        target = self.path(session_id)
        if not target.exists():
            raise NotFoundError(f"sessão não encontrada: {session_id}")
        with target.open(encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        meta = SessionMeta.model_validate(json.loads(lines[0])["meta"])
        messages = [Message.model_validate_json(line) for line in lines[1:]]
        return Session(meta, messages)

    def list(self) -> list[SessionMeta]:
        if not self._dir.exists():
            return []
        metas: list[SessionMeta] = []
        for f in self._dir.glob("*.jsonl"):
            with f.open(encoding="utf-8") as fh:
                first = fh.readline()
            try:
                metas.append(SessionMeta.model_validate(json.loads(first)["meta"]))
            except (ValueError, KeyError):
                continue
        return sorted(metas, key=lambda m: m.updated_at, reverse=True)
