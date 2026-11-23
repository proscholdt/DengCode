"""Leitura dos artefatos do dbt (``manifest.json`` e ``run_results.json``).

Funções puras sobre dicionários já carregados: não rodam o dbt nem tocam disco.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from dengcode_core.ports.models import LineageEdge
from dengcode_core.ports.transform import ModelInfo, NodeResult

NodeStatus = Literal["success", "error", "fail", "warn", "skipped", "pass", "unknown"]

# status do dbt (RunStatus, TestStatus, FreshnessStatus) → status canônico
_STATUS_MAP: dict[str, NodeStatus] = {
    "success": "success",
    "pass": "pass",
    "error": "error",
    "runtime error": "error",
    "fail": "fail",
    "warn": "warn",
    "partial success": "warn",
    "skipped": "skipped",
    "no-op": "skipped",
}

# nós que viram relação no banco (podem ser origem de lineage)
_RELATION_TYPES = frozenset({"model", "seed", "snapshot"})


def map_status(status: str | None) -> NodeStatus:
    if not status:
        return "unknown"
    return _STATUS_MAP.get(status.strip().lower(), "unknown")


def _dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[Any], value) if isinstance(v, str)]


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def nodes(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: _dict(v) for k, v in _dict(manifest.get("nodes")).items()}


def sources(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: _dict(v) for k, v in _dict(manifest.get("sources")).items()}


def models(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Nós de ``resource_type == "model"``, por ``unique_id``."""
    return {k: v for k, v in nodes(manifest).items() if v.get("resource_type") == "model"}


def node_relation(node: Mapping[str, Any]) -> str | None:
    """``schema.alias`` em minúsculas (sem o database, que no dbt-duckdb é o arquivo)."""
    schema = _opt_str(node.get("schema"))
    name = _opt_str(node.get("alias")) or _opt_str(node.get("name"))
    if schema is None or name is None:
        return None
    return f"{schema}.{name}".lower()


def source_relation(source: Mapping[str, Any]) -> str | None:
    """``schema.identifier`` (ou ``schema.name``) em minúsculas."""
    schema = _opt_str(source.get("schema"))
    name = _opt_str(source.get("identifier")) or _opt_str(source.get("name"))
    if schema is None or name is None:
        return None
    return f"{schema}.{name}".lower()


def source_label(source: Mapping[str, Any]) -> str:
    """Como o dbt se refere à fonte: ``source_name.name``."""
    name = _opt_str(source.get("name")) or ""
    group = _opt_str(source.get("source_name"))
    return f"{group}.{name}" if group else name


def _dependency_names(
    node: Mapping[str, Any],
    all_nodes: Mapping[str, Mapping[str, Any]],
    all_sources: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    names: list[str] = []
    for dep in _str_list(_dict(node.get("depends_on")).get("nodes")):
        if dep in all_nodes:
            dep_name = _opt_str(all_nodes[dep].get("name"))
            if dep_name:
                names.append(dep_name)
        elif dep in all_sources:
            names.append(source_label(all_sources[dep]))
    return names


def model_info(
    node: Mapping[str, Any],
    all_nodes: Mapping[str, Mapping[str, Any]],
    all_sources: Mapping[str, Mapping[str, Any]],
) -> ModelInfo:
    path = _opt_str(node.get("original_file_path"))
    return ModelInfo(
        name=str(node.get("name", "")),
        unique_id=str(node.get("unique_id", "")),
        # o manifest gerado no Windows usa "\"; o contrato usa "/"
        path=path.replace("\\", "/") if path else None,
        relation=node_relation(node),
        materialized=_opt_str(_dict(node.get("config")).get("materialized")),
        depends_on=_dependency_names(node, all_nodes, all_sources),
        columns=list(_dict(node.get("columns")).keys()),
        description=_opt_str(node.get("description")),
    )


def model_infos(manifest: Mapping[str, Any]) -> list[ModelInfo]:
    all_nodes = nodes(manifest)
    all_sources = sources(manifest)
    infos = [
        model_info(node, all_nodes, all_sources)
        for node in all_nodes.values()
        if node.get("resource_type") == "model"
    ]
    return sorted(infos, key=lambda m: m.name)


def compiled_models(manifest: Mapping[str, Any]) -> dict[str, str]:
    """SQL compilado por nome de modelo (e por ``unique_id``)."""
    out: dict[str, str] = {}
    for uid, node in models(manifest).items():
        code = node.get("compiled_code")
        if not isinstance(code, str):
            continue
        out[uid] = code
        name = _opt_str(node.get("name"))
        if name:
            out[name] = code
    return out


def lineage_edges(manifest: Mapping[str, Any]) -> list[LineageEdge]:
    """Arestas tabela → tabela a partir de ``depends_on.nodes`` dos modelos."""
    all_nodes = nodes(manifest)
    all_sources = sources(manifest)
    edges: list[LineageEdge] = []
    seen: set[tuple[str, str]] = set()
    for node in models(manifest).values():
        target = node_relation(node)
        if target is None:
            continue
        via = _opt_str(node.get("name"))
        for dep in _str_list(_dict(node.get("depends_on")).get("nodes")):
            if dep in all_sources:
                source = source_relation(all_sources[dep])
            elif dep in all_nodes and all_nodes[dep].get("resource_type") in _RELATION_TYPES:
                source = node_relation(all_nodes[dep])
            else:
                source = None
            if source is None or (source, target) in seen:
                continue
            seen.add((source, target))
            edges.append(LineageEdge(source=source, target=target, via=via))
    return edges


def _names(manifest: Mapping[str, Any] | None) -> dict[str, str]:
    if manifest is None:
        return {}
    out: dict[str, str] = {}
    for uid, node in (nodes(manifest) | sources(manifest)).items():
        name = _opt_str(node.get("name"))
        if name:
            out[uid] = name
    return out


def _node_name(unique_id: str, names: Mapping[str, str]) -> str:
    if unique_id in names:
        return names[unique_id]
    # "model.projeto.nome" / "test.projeto.nome.hash"
    parts = unique_id.split(".")
    return parts[2] if len(parts) >= 3 else unique_id


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def node_results(
    run_results: Mapping[str, Any], manifest: Mapping[str, Any] | None = None
) -> list[NodeResult]:
    """Converte ``run_results.json`` em ``NodeResult``s (ordem original)."""
    raw = run_results.get("results")
    if not isinstance(raw, list):
        return []
    names = _names(manifest)
    out: list[NodeResult] = []
    for item in cast(list[Any], raw):
        entry = _dict(item)
        uid = _opt_str(entry.get("unique_id"))
        if uid is None:
            continue
        status = entry.get("status")
        out.append(
            NodeResult(
                name=_node_name(uid, names),
                status=map_status(status if isinstance(status, str) else None),
                message=_opt_str(entry.get("message")),
                elapsed_s=_opt_float(entry.get("execution_time")),
                failures=_opt_int(entry.get("failures")),
            )
        )
    return out
