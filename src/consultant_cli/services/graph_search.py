from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from consultant_cli.infrastructure.store import RepositoryPaths, atomic_write_json


WORD = re.compile(r"[0-9a-zа-яё_]{2,}", re.IGNORECASE)
DEFAULT_RELATIONS = (
    "entry_doc",
    "clarification",
    "clarifies_field",
    "parent_child",
    "child_parent",
    "references",
    "has_field",
    "field_of",
    "creates_on_basis",
    "can_be_created_on_basis_of",
    "has_command",
    "opened_by_command",
    "describes_metadata",
    "documented_by",
    "requires",
    "required_by",
)


# Common business labels that are not technical metadata names.  Every alias
# must point to an exact L3 document and an L4 node explaining the operation.
DOCUMENT_TECH_ALIASES = {
    "СписаниеЗапасов": {
        "technical_name": "ВнутреннееПотребление",
        "business_label": "Списание запасов",
        "replacement": "Внутреннее потребление (операция «Списание на расходы»)",
        "evidence_node_id": (
            "007--6. Склад и доставка/011--6.11. Внутреннее товародвижение и "
            "внутреннее потребление/операция_внутреннего_потребления_товаров_и_работ"
        ),
    }
}


def _terms(value: str) -> list[str]:
    return list(dict.fromkeys(WORD.findall(value.casefold())))[:24]


def _fts_expression(value: str, strategy: str = "any") -> str:
    terms = _terms(value)
    if not terms:
        return '"erp"'
    operator = " AND " if strategy == "all" else " OR "
    return operator.join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)


def _compact_node(row: sqlite3.Row, preview_chars: int = 900) -> dict[str, Any]:
    metadata = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, IndexError):
        pass
    return {
        "id": row["id"],
        "canonical_id": row["canonical_id"] or row["id"],
        "title": row["title"],
        "path": row["path"],
        "layer": int(row["layer"]),
        "node_type": row["node_type"],
        "level": int(row["level"]),
        "preview": str(row["preview"] or "")[:preview_chars],
        "source_ref": metadata.get("source_xml") or row["path"],
        "metadata": metadata,
    }


class GraphSearchService:
    """Python-only reader for Yana's published four-layer SQLite graph."""

    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.root, self.database_path = self._discover_graph()

    def _discover_graph(self) -> tuple[Path, Path]:
        explicit = os.environ.get("ERP_GRAPH_DATABASE")
        if explicit:
            database = Path(explicit).expanduser()
            if database.is_file():
                return database.parent, database.resolve()
        for root in self.paths.installed_graphs():
            for database in (
                root / "graph_rag_data" / "erp_graph_mcp.sqlite",
                root / "erp_graph_mcp.sqlite",
            ):
                if database.is_file():
                    return root.resolve(), database.resolve()
        candidates = [
            self.paths.root,
            self.paths.root.parent / "RAGAgent",
        ]
        for root in candidates:
            database = root / "graph_rag_data" / "erp_graph_mcp.sqlite"
            if database.is_file():
                return root.resolve(), database.resolve()
        return self.paths.root, self.paths.root / "graph_rag_data" / "erp_graph_mcp.sqlite"

    @property
    def available(self) -> bool:
        return self.database_path.is_file()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        uri = f"file:{self.database_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        if not self.available:
            return {"ready": False, "index": str(self.database_path)}
        with self._connect() as connection:
            metadata = dict(connection.execute("SELECT key, value FROM graph_meta"))
        return {
            "ready": True,
            "index": str(self.database_path),
            "schema_version": int(metadata.get("schema_version", 0)),
            "product": metadata.get("product", ""),
            "release": metadata.get("product_version", ""),
            "nodes": int(metadata.get("nodes", 0)),
            "edges": int(metadata.get("edges", 0)),
            "semantic_postings": int(metadata.get("semantic_postings", 0)),
            "l3_l4_logical_links": int(metadata.get("l3_l4_logical_links", 0)),
        }

    def search(
        self,
        query: str,
        *,
        layers: Iterable[int] = (),
        node_types: Iterable[str] = (),
        limit: int = 12,
        strategy: str = "any",
    ) -> dict[str, Any]:
        if not self.available:
            return {"query": query, "ready": False, "results": []}
        layer_values = list(layers)
        type_values = list(node_types)
        candidate_limit = min(200, max(limit * 5, 40))
        with self._connect() as connection:
            conditions = ["nodes_fts MATCH ?"]
            parameters: list[Any] = [_fts_expression(query, strategy)]
            if layer_values:
                conditions.append(f"n.layer IN ({','.join('?' for _ in layer_values)})")
                parameters.extend(layer_values)
            if type_values:
                conditions.append(f"n.node_type IN ({','.join('?' for _ in type_values)})")
                parameters.extend(type_values)
            lexical = connection.execute(
                "SELECT n.*, bm25(nodes_fts, 8.0, 2.0, 4.0, 1.0) AS rank "
                "FROM nodes_fts JOIN nodes n ON n.node_pk=nodes_fts.rowid "
                f"WHERE {' AND '.join(conditions)} ORDER BY rank, n.layer, n.title LIMIT ?",
                [*parameters, candidate_limit],
            ).fetchall()

            requested = Counter(_terms(query))
            semantic: list[sqlite3.Row] = []
            if requested:
                terms = list(requested)
                term_rows = connection.execute(
                    f"SELECT term_idx,term,idf FROM semantic_terms WHERE term IN ({','.join('?' for _ in terms)})",
                    terms,
                ).fetchall()
                raw = [
                    (int(row["term_idx"]), (1 + math.log(requested[row["term"]])) * float(row["idf"]))
                    for row in term_rows
                ]
                norm = math.sqrt(sum(weight * weight for _, weight in raw)) or 1.0
                weighted = [(term_idx, weight / norm) for term_idx, weight in raw]
                if weighted:
                    values_sql = ",".join("(?,?)" for _ in weighted)
                    semantic_conditions: list[str] = []
                    semantic_parameters: list[Any] = [item for pair in weighted for item in pair]
                    if layer_values:
                        semantic_conditions.append(f"n.layer IN ({','.join('?' for _ in layer_values)})")
                        semantic_parameters.extend(layer_values)
                    if type_values:
                        semantic_conditions.append(f"n.node_type IN ({','.join('?' for _ in type_values)})")
                        semantic_parameters.extend(type_values)
                    where = "WHERE " + " AND ".join(semantic_conditions) if semantic_conditions else ""
                    semantic = connection.execute(
                        f"WITH query_terms(term_idx,query_weight) AS (VALUES {values_sql}) "
                        "SELECT n.*, SUM(p.weight*q.query_weight) AS semantic_score "
                        "FROM query_terms q JOIN semantic_postings p ON p.term_idx=q.term_idx "
                        f"JOIN nodes n ON n.node_pk=p.node_pk {where} GROUP BY n.node_pk "
                        "HAVING semantic_score>0 ORDER BY semantic_score DESC, n.layer, n.title LIMIT ?",
                        [*semantic_parameters, candidate_limit],
                    ).fetchall()

        # Keep the same deterministic fusion used by the published Yana runtime.
        semantic_values = [float(row["semantic_score"]) for row in semantic]
        minimum = min(semantic_values, default=0.0)
        maximum = max(semantic_values, default=0.0)
        semantic_normalized: dict[str, float] = {}
        for row in semantic:
            value = float(row["semantic_score"])
            semantic_normalized[row["id"]] = (
                (value - minimum) / (maximum - minimum)
                if maximum > minimum
                else (1.0 if value > 0 else 0.0)
            )
        merged: dict[str, sqlite3.Row] = {row["id"]: row for row in lexical}
        merged.update({row["id"]: row for row in semantic})
        lexical_rank = {row["id"]: index + 1 for index, row in enumerate(lexical)}
        semantic_rank = {row["id"]: index + 1 for index, row in enumerate(semantic)}
        phrase = query.strip().casefold()
        results = []
        for node_id, row in merged.items():
            lr = lexical_rank.get(node_id)
            sr = semantic_rank.get(node_id)
            lexical_score = 1 / lr if lr else 0.0
            semantic_score = semantic_normalized.get(node_id, 0.0)
            exact_title = 1.0 if phrase and phrase in str(row["title"]).casefold() else 0.0
            reciprocal = (1 / (60 + lr) if lr else 0.0) + (1 / (60 + sr) if sr else 0.0)
            item = _compact_node(row)
            item.update(
                lexical_rank=lr,
                semantic_rank=sr,
                rerank_score=round(
                    0.32 * lexical_score
                    + 0.43 * semantic_score
                    + 0.15 * exact_title
                    + 0.10 * reciprocal,
                    8,
                ),
                channels=[name for name, present in (("fts5", lr), ("semantic", sr)) if present],
            )
            results.append(item)
        results.sort(key=lambda item: (-item["rerank_score"], item["layer"], item["title"]))
        return {"query": query, "ready": True, "mode": "hybrid", "results": results[:limit]}

    def expand(
        self,
        seeds: Iterable[str],
        *,
        relations: Iterable[str] = DEFAULT_RELATIONS,
        limit: int = 120,
    ) -> dict[str, Any]:
        seed_values = list(dict.fromkeys(str(value) for value in seeds if value))[:20]
        if not self.available or not seed_values:
            return {"seeds": seed_values, "nodes": [], "edges": []}
        relation_values = list(relations)
        with self._connect() as connection:
            seed_rows = connection.execute(
                f"SELECT * FROM nodes WHERE id IN ({','.join('?' for _ in seed_values)}) "
                f"OR canonical_id IN ({','.join('?' for _ in seed_values)})",
                [*seed_values, *seed_values],
            ).fetchall()
            resolved = [row["id"] for row in seed_rows]
            if not resolved:
                return {"seeds": seed_values, "nodes": [], "edges": []}
            relation_sql = f" AND e.relation IN ({','.join('?' for _ in relation_values)})" if relation_values else ""
            sql = (
                "SELECT ns.id source, nt.id target, e.relation, e.edge_key, e.weight, "
                "e.properties_json, e.evidence_json FROM edges e "
                "JOIN nodes ns ON ns.node_pk=e.source_pk JOIN nodes nt ON nt.node_pk=e.target_pk "
                f"WHERE (ns.id IN ({','.join('?' for _ in resolved)}) OR nt.id IN ({','.join('?' for _ in resolved)}))"
                f"{relation_sql} LIMIT ?"
            )
            rows = connection.execute(
                sql, [*resolved, *resolved, *relation_values, max(200, limit * 8)]
            ).fetchall()
            node_ids = list(dict.fromkeys([*resolved, *[row["source"] for row in rows], *[row["target"] for row in rows]]))[:limit]
            nodes = connection.execute(
                f"SELECT * FROM nodes WHERE id IN ({','.join('?' for _ in node_ids)})", node_ids
            ).fetchall()
        found = set(node_ids)
        edges = []
        for row in rows:
            if row["source"] not in found or row["target"] not in found:
                continue
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except json.JSONDecodeError:
                evidence = {}
            edges.append(
                {
                    "id": row["edge_key"],
                    "source": row["source"],
                    "target": row["target"],
                    "relation": row["relation"],
                    "weight": float(row["weight"]),
                    "source_ref": evidence.get("source_path") or evidence.get("source_ref") or "",
                    "evidence": evidence,
                }
            )
        return {"seeds": resolved, "nodes": [_compact_node(row) for row in nodes], "edges": edges}

    def document_flow_errors(self, payload: dict[str, Any]) -> list[str]:
        """Reject invented document IDs in the prominent answer chain."""
        if not self.available:
            return []
        documents = [
            document
            for branch in payload.get("document_flow", [])
            if isinstance(branch, dict)
            for document in branch.get("documents", [])
            if isinstance(document, dict)
        ]
        node_ids = list(
            dict.fromkeys(str(document.get("node_id") or "") for document in documents)
        )
        node_ids = [node_id for node_id in node_ids if node_id]
        if not node_ids:
            return ["document_flow не содержит ERP node_id"] if documents else []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id,canonical_id,layer,node_type,title,metadata_json FROM nodes "
                f"WHERE id IN ({','.join('?' for _ in node_ids)}) "
                f"OR canonical_id IN ({','.join('?' for _ in node_ids)})",
                [*node_ids, *node_ids],
            ).fetchall()
        found: dict[str, sqlite3.Row] = {}
        for row in rows:
            found[str(row["id"])] = row
            found[str(row["canonical_id"])] = row
        errors = []
        for document in documents:
            node_id = str(document.get("node_id") or "")
            row = found.get(node_id)
            if row is None:
                errors.append(f"Неизвестный документ в document_flow: {node_id or document.get('name')}")
            else:
                try:
                    metadata_type = str(
                        json.loads(row["metadata_json"] or "{}").get("metadata_type", "")
                    )
                except json.JSONDecodeError:
                    metadata_type = ""
                is_document = (
                    str(row["node_type"]) == "document"
                    or metadata_type.casefold() == "document"
                    or "/Documents/" in str(row["id"])
                )
                if int(row["layer"]) == 3 and is_document:
                    if str(document.get("name") or "").strip().casefold() != str(
                        row["title"]
                    ).strip().casefold():
                        errors.append(
                            f"Название document_flow не совпадает с графом для {node_id}: "
                            f"ожидается «{row['title']}»"
                        )
                    continue
                errors.append(
                    f"Узел document_flow не является L3 document: {node_id} "
                    f"(L{row['layer']}/{row['node_type']})"
                )
        return errors

    def normalize_document_flow(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        """Resolve conventional 1C aliases to exact, unambiguous L3 documents.

        API roles often emit platform-style IDs such as ``Document.ЗаказКлиента``
        while the published graph stores ``ERPcode/Documents/ЗаказКлиента``.  A
        repair is allowed only when a single document remains after exact ID and
        exact human-title matching.  The resolved graph node is also persisted as
        a source, preserving ``source_ref`` in the generated artifact.
        """
        if not self.available:
            return []
        documents = [
            document
            for branch in payload.get("document_flow", []) or []
            if isinstance(branch, dict)
            for document in branch.get("documents", []) or []
            if isinstance(document, dict)
        ]
        if not documents:
            return []

        def clean_title(value: str, *, strip_context: bool = False) -> str:
            normalized = " ".join(value.casefold().replace("ё", "е").split())
            if normalized.startswith("документ "):
                normalized = normalized[len("документ ") :]
            if strip_context:
                normalized = re.sub(r"\s*\([^()]*\)\s*$", "", normalized)
            return normalized.strip()

        node_ids = list(
            dict.fromkeys(
                str(document.get("node_id") or "").strip()
                for document in documents
                if str(document.get("node_id") or "").strip()
            )
        )
        with self._connect() as connection:
            direct_rows = (
                connection.execute(
                    f"SELECT id,canonical_id,layer,node_type,title,path,metadata_json "
                    f"FROM nodes WHERE id IN ({','.join('?' for _ in node_ids)}) "
                    f"OR canonical_id IN ({','.join('?' for _ in node_ids)})",
                    [*node_ids, *node_ids],
                ).fetchall()
                if node_ids
                else []
            )
            found: dict[str, sqlite3.Row] = {}
            for row in direct_rows:
                found[str(row["id"])] = row
                found[str(row["canonical_id"])] = row

            resolved: list[tuple[dict[str, Any], sqlite3.Row, str, str]] = []
            text_aliases: dict[str, str] = {}
            title_document_rows: list[sqlite3.Row] | None = None
            for document in documents:
                original_id = str(document.get("node_id") or "").strip()
                row = found.get(original_id)
                alias_evidence = ""
                alias_label = ""
                if row is None:
                    match = re.fullmatch(r"(?:Document|Документ)[./]([^/]+)", original_id)
                    if match:
                        emitted_name = match.group(1)
                        alias = DOCUMENT_TECH_ALIASES.get(emitted_name)
                        technical_name = (
                            str(alias["technical_name"]) if alias else emitted_name
                        )
                        alias_evidence = (
                            str(alias.get("evidence_node_id") or "") if alias else ""
                        )
                        if alias:
                            alias_label = str(alias.get("business_label") or "")
                            replacement = str(alias.get("replacement") or "")
                            if alias_label and replacement:
                                text_aliases[alias_label] = replacement
                        base_id = f"ERPcode/Documents/{technical_name}"
                        candidates = connection.execute(
                            "SELECT id,canonical_id,layer,node_type,title,path,metadata_json "
                            "FROM nodes WHERE layer=3 AND "
                            "(id=? OR (id LIKE ? AND id NOT LIKE ?))",
                            (base_id, base_id + "%", base_id + "/%"),
                        ).fetchall()
                        candidates = [
                            candidate
                            for candidate in candidates
                            if str(candidate["node_type"]) == "document"
                            or str(
                                json.loads(candidate["metadata_json"] or "{}").get(
                                    "metadata_type", ""
                                )
                            ).casefold()
                            == "document"
                        ]
                        expected_title = clean_title(
                            str(document.get("name") or ""), strip_context=True
                        )
                        title_matches = [
                            candidate
                            for candidate in candidates
                            if clean_title(str(candidate["title"] or "")) == expected_title
                        ]
                        if len(title_matches) == 1:
                            row = title_matches[0]
                        elif len(candidates) == 1:
                            row = candidates[0]
                if row is None:
                    # Project-query writers sometimes put an evidence/source ID
                    # into node_id while the human name still identifies a real
                    # document exactly. Resolve that deterministic case by the
                    # graph title; settings and catalogs deliberately remain
                    # unresolved and can be pruned from a query-only flow.
                    if title_document_rows is None:
                        title_document_rows = connection.execute(
                            "SELECT id,canonical_id,layer,node_type,title,path,metadata_json "
                            "FROM nodes WHERE layer=3 AND "
                            "(node_type='document' OR id LIKE '%/Documents/%')"
                        ).fetchall()
                    expected_title = clean_title(
                        str(document.get("name") or ""), strip_context=True
                    )
                    title_matches = [
                        candidate
                        for candidate in title_document_rows
                        if clean_title(str(candidate["title"] or "")) == expected_title
                    ]
                    if len(title_matches) == 1:
                        row = title_matches[0]
                if row is not None:
                    resolved.append((document, row, alias_evidence, alias_label))

            graph_release_row = connection.execute(
                "SELECT value FROM graph_meta WHERE key='product_version'"
            ).fetchone()
            graph_release = str(graph_release_row[0]) if graph_release_row else "2.5"

            knowledge_rows: dict[str, sqlite3.Row] = {}
            evidence_ids = [item[2] for item in resolved if item[2]]
            if evidence_ids:
                rows = connection.execute(
                    f"SELECT id,canonical_id,title,path,layer,node_type,metadata_json "
                    f"FROM nodes WHERE id IN ({','.join('?' for _ in evidence_ids)})",
                    evidence_ids,
                ).fetchall()
                knowledge_rows = {str(row["id"]): row for row in rows}

        repairs: list[dict[str, str]] = []
        sources = payload.setdefault("sources", [])
        if not isinstance(sources, list):
            return []
        source_ids = {
            str(source.get("id") or "")
            for source in sources
            if isinstance(source, dict)
        }

        def append_graph_source(row: sqlite3.Row, status: str) -> str:
            canonical_id = str(row["canonical_id"] or row["id"])
            source_id = f"graph:{canonical_id}"
            if source_id not in source_ids:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                source_ref = str(
                    metadata.get("source_xml")
                    or metadata.get("source_path")
                    or row["path"]
                    or row["id"]
                )
                sources.append(
                    {
                        "id": source_id,
                        "title": str(row["title"] or row["id"]),
                        "local_ref": "",
                        "url": "",
                        "product": "1С:ERP Управление предприятием 2",
                        "release": graph_release,
                        "verification_status": status,
                        "notes": "Узел опубликованного четырёхслойного ERP-графа.",
                        "source_ref": source_ref,
                        "node_id": str(row["id"]),
                        "edge_ids": [],
                    }
                )
                source_ids.add(source_id)
            return source_id

        alias_source_ids: dict[str, list[str]] = {}
        for document, row, alias_evidence, alias_label in resolved:
            if int(row["layer"]) != 3:
                continue
            try:
                metadata_type = str(
                    json.loads(row["metadata_json"] or "{}").get("metadata_type", "")
                )
            except json.JSONDecodeError:
                metadata_type = ""
            is_document = (
                str(row["node_type"]) == "document"
                or metadata_type.casefold() == "document"
                or "/Documents/" in str(row["id"])
            )
            if not is_document:
                continue
            original_id = str(document.get("node_id") or "").strip()
            canonical_node_id = str(row["id"])
            if original_id != canonical_node_id:
                document["node_id"] = canonical_node_id
                repairs.append(
                    {
                        "field": "node_id",
                        "from": original_id,
                        "to": canonical_node_id,
                    }
                )
            current = str(document.get("name") or "").strip()
            canonical = str(row["title"] or "").strip()
            if canonical and current != canonical:
                document["name"] = canonical
                repairs.append(
                    {
                        "field": "name",
                        "from": current,
                        "to": canonical,
                    }
                )
            evidence_refs = document.setdefault("evidence_refs", [])
            if isinstance(evidence_refs, list):
                metadata_source_id = append_graph_source(row, "verified_metadata")
                if metadata_source_id not in evidence_refs:
                    evidence_refs.append(metadata_source_id)
                    repairs.append(
                        {
                            "field": "evidence_refs",
                            "from": "",
                            "to": metadata_source_id,
                        }
                    )
                if alias_label:
                    alias_source_ids.setdefault(alias_label, []).append(metadata_source_id)
                knowledge_row = knowledge_rows.get(alias_evidence)
                if knowledge_row is not None:
                    knowledge_source_id = append_graph_source(knowledge_row, "verified")
                    if knowledge_source_id not in evidence_refs:
                        evidence_refs.append(knowledge_source_id)
                        repairs.append(
                            {
                                "field": "evidence_refs",
                                "from": "",
                                "to": knowledge_source_id,
                            }
                        )
                    if alias_label:
                        alias_source_ids.setdefault(alias_label, []).append(
                            knowledge_source_id
                        )

        for step in payload.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            step_text = json.dumps(step, ensure_ascii=False).casefold()
            evidence_refs = step.get("evidence_refs")
            if not isinstance(evidence_refs, list):
                continue
            for label, source_refs in alias_source_ids.items():
                if label.casefold() not in step_text:
                    continue
                for source_id in dict.fromkeys(source_refs):
                    if source_id not in evidence_refs:
                        evidence_refs.append(source_id)
                        repairs.append(
                            {
                                "field": f"steps.{step.get('id')}.evidence_refs",
                                "from": "",
                                "to": source_id,
                            }
                        )

        protected_keys = {
            "id",
            "node_id",
            "source_ref",
            "local_ref",
            "url",
            "evidence_refs",
            "semantic_relation_refs",
            "edge_ids",
        }

        def replace_aliases(value: Any, key: str = "") -> Any:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    if child_key not in protected_keys:
                        value[child_key] = replace_aliases(child_value, child_key)
                return value
            if isinstance(value, list):
                for index, child_value in enumerate(value):
                    value[index] = replace_aliases(child_value, key)
                return value
            if isinstance(value, str):
                updated = value
                for label, replacement in text_aliases.items():
                    updated = re.sub(
                        re.escape(label), replacement, updated, flags=re.IGNORECASE
                    )
                if updated != value:
                    repairs.append({"field": key or "text", "from": value, "to": updated})
                return updated
            return value

        replace_aliases(payload)
        return repairs

    def prune_non_document_flow(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        """Remove unresolved/non-document entries from a project-query flow only.

        The prominent ``document_flow`` contract is intentionally strict.  A
        saved answer to a narrower project question may also describe settings,
        catalogs and functional options; those belong to steps, not to the
        document chain.  Callers must first run ``normalize_document_flow`` so
        any deterministically named real documents are retained.
        """
        if not self.available:
            return []
        branches = payload.get("document_flow", [])
        if not isinstance(branches, list):
            return []
        documents = [
            document
            for branch in branches
            if isinstance(branch, dict)
            for document in branch.get("documents", []) or []
            if isinstance(document, dict)
        ]
        node_ids = list(
            dict.fromkeys(
                str(document.get("node_id") or "").strip()
                for document in documents
                if str(document.get("node_id") or "").strip()
            )
        )
        valid_ids: set[str] = set()
        if node_ids:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT id,canonical_id,layer,node_type,metadata_json FROM nodes "
                    f"WHERE id IN ({','.join('?' for _ in node_ids)}) "
                    f"OR canonical_id IN ({','.join('?' for _ in node_ids)})",
                    [*node_ids, *node_ids],
                ).fetchall()
            for row in rows:
                try:
                    metadata_type = str(
                        json.loads(row["metadata_json"] or "{}").get("metadata_type", "")
                    )
                except json.JSONDecodeError:
                    metadata_type = ""
                if int(row["layer"]) == 3 and (
                    str(row["node_type"]) == "document"
                    or metadata_type.casefold() == "document"
                    or "/Documents/" in str(row["id"])
                ):
                    valid_ids.add(str(row["id"]))
                    valid_ids.add(str(row["canonical_id"]))

        repairs: list[dict[str, str]] = []
        kept_branches: list[dict[str, Any]] = []
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            kept_documents = []
            for document in branch.get("documents", []) or []:
                if not isinstance(document, dict):
                    continue
                node_id = str(document.get("node_id") or "").strip()
                if node_id in valid_ids:
                    kept_documents.append(document)
                else:
                    repairs.append(
                        {
                            "field": "document_flow.documents",
                            "from": node_id or str(document.get("name") or ""),
                            "to": "removed_non_document",
                        }
                    )
            if kept_documents:
                branch["documents"] = kept_documents
                kept_branches.append(branch)
            else:
                repairs.append(
                    {
                        "field": "document_flow.branch",
                        "from": str(branch.get("title") or ""),
                        "to": "removed_empty_branch",
                    }
                )
        payload["document_flow"] = kept_branches
        return repairs

    def project_context(self, project_id: str, request: str, revision: int) -> str:
        target = (
            self.paths.results
            / project_id
            / "agent_artifacts"
            / f"graph-context-r{revision:03d}.json"
        )
        request_sha256 = hashlib.sha256(request.encode("utf-8")).hexdigest()
        graph_signature = "missing"
        if self.database_path.is_file():
            stat = self.database_path.stat()
            graph_signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        if target.is_file():
            try:
                cached = json.loads(target.read_text(encoding="utf-8"))
                if (
                    cached.get("request_sha256") == request_sha256
                    and cached.get("graph_signature") == graph_signature
                ):
                    return json.dumps(cached, ensure_ascii=False, separators=(",", ":"))
            except (OSError, json.JSONDecodeError):
                pass
        intents = [line.strip(" #-*\t") for line in request.splitlines() if len(line.strip()) >= 12]
        intents = list(dict.fromkeys([request[:1200], *intents]))[:6]
        searches = [self.search(intent, limit=10) for intent in intents]
        seeds = []
        for result in searches:
            seeds.extend(item["id"] for item in result.get("results", [])[:4])
        expansion = self.expand(seeds)
        payload = {
            "schema_version": 1,
            "project_id": project_id,
            "revision": revision,
            "request_sha256": request_sha256,
            "graph_signature": graph_signature,
            "graph": self.status(),
            "searches": searches,
            "expansion": expansion,
            "evidence_policy": (
                "Search rank is candidate-only. Exact metadata requires L3; business behavior "
                "requires L4 or an explicit user decision. Preserve node id, edge relation and source_ref."
            ),
        }
        atomic_write_json(target, payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
