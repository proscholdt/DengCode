"""Checkpoints: cópia do arquivo antes de cada escrita, para ``undo``.

Layout: ``<workspace>/.dengcode/checkpoints/<sessão>/``
  - ``manifest.jsonl``: uma linha por escrita (caminho, se existia, arquivo de backup)
  - ``NNNNNN.bak``: conteúdo anterior
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Checkpoint:
    seq: int
    path: Path
    existed: bool
    backup: Path | None
    created_at: str


def _line(cp: Checkpoint) -> str:
    data = {
        "seq": cp.seq,
        "path": str(cp.path),
        "existed": cp.existed,
        "backup": str(cp.backup) if cp.backup else None,
        "created_at": cp.created_at,
    }
    return json.dumps(data, ensure_ascii=False) + "\n"


class CheckpointStore:
    def __init__(self, base_dir: Path) -> None:
        self._dir = base_dir

    @classmethod
    def for_session(cls, workspace_root: Path, session_id: str) -> CheckpointStore:
        return cls(workspace_root / ".dengcode" / "checkpoints" / session_id)

    @property
    def manifest(self) -> Path:
        return self._dir / "manifest.jsonl"

    def entries(self) -> list[Checkpoint]:
        if not self.manifest.exists():
            return []
        out: list[Checkpoint] = []
        with self.manifest.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                raw = json.loads(line)
                out.append(
                    Checkpoint(
                        seq=int(raw["seq"]),
                        path=Path(raw["path"]),
                        existed=bool(raw["existed"]),
                        backup=Path(raw["backup"]) if raw.get("backup") else None,
                        created_at=str(raw["created_at"]),
                    )
                )
        return out

    def save(self, path: Path) -> Checkpoint:
        """Registra o estado atual de ``path`` antes de uma escrita."""
        self._dir.mkdir(parents=True, exist_ok=True)
        seq = len(self.entries()) + 1
        existed = path.exists()
        backup: Path | None = None
        if existed:
            backup = self._dir / f"{seq:06d}.bak"
            shutil.copy2(path, backup)
        cp = Checkpoint(seq, path, existed, backup, datetime.now(UTC).isoformat())
        with self.manifest.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(_line(cp))
        return cp

    def undo(self) -> Checkpoint | None:
        """Restaura a última escrita e a remove do manifesto."""
        entries = self.entries()
        if not entries:
            return None
        last = entries[-1]
        if last.existed and last.backup is not None:
            last.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(last.backup, last.path)
            last.backup.unlink(missing_ok=True)
        else:
            last.path.unlink(missing_ok=True)
        remaining = entries[:-1]
        with self.manifest.open("w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(_line(cp) for cp in remaining)
        return last


def latest_session_store(workspace_root: Path) -> CheckpointStore | None:
    """Store da sessão com o checkpoint mais recente (para ``dengcode undo``)."""
    base = workspace_root / ".dengcode" / "checkpoints"
    if not base.exists():
        return None
    manifests = [m for m in base.glob("*/manifest.jsonl") if m.stat().st_size > 0]
    if not manifests:
        return None
    newest = max(manifests, key=lambda m: m.stat().st_mtime)
    return CheckpointStore(newest.parent)
