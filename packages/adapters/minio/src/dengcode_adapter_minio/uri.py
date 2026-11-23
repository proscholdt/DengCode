"""Interpretação de URIs S3 (``s3://bucket/chave`` ou ``bucket/chave``)."""

from __future__ import annotations

from dataclasses import dataclass

from dengcode_core.errors import AdapterError

_SCHEMES = ("s3://", "s3a://")


@dataclass(frozen=True)
class S3Location:
    """Bucket + chave já normalizados (chave sem barras nas pontas)."""

    bucket: str
    key: str

    def uri(self, *, is_dir: bool = False) -> str:
        """URI canônica ``s3://bucket/chave`` (com ``/`` final para prefixos)."""
        if not self.key:
            return f"s3://{self.bucket}/"
        return f"s3://{self.bucket}/{self.key}{'/' if is_dir else ''}"


def parse_uri(uri: str, *, default_bucket: str | None = None) -> S3Location:
    """Separa bucket e chave.

    - ``s3://bucket/chave`` (ou ``s3a://``) e ``bucket/chave``: o primeiro
      segmento é sempre o bucket.
    - ``""``, ``"."`` ou ``"/"``: raiz do bucket padrão (se configurado).
    """
    text = uri.strip().replace("\\", "/")
    lowered = text.lower()
    scheme = next((s for s in _SCHEMES if lowered.startswith(s)), None)
    if scheme is not None:
        text = text[len(scheme) :]
    elif "://" in text:
        raise AdapterError(f"esquema não suportado pelo adapter minio: {uri!r}")
    text = text.strip("/")
    if scheme is None and text in ("", "."):
        if not default_bucket:
            raise AdapterError("informe o bucket (s3://bucket/chave): nenhum bucket padrão")
        return S3Location(default_bucket, "")
    bucket, _, key = text.partition("/")
    if not bucket:
        raise AdapterError(f"URI sem bucket: {uri!r}")
    parts = [p for p in key.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise AdapterError(f"chave com '.' ou '..' não é aceita: {uri!r}")
    return S3Location(bucket, "/".join(parts))
