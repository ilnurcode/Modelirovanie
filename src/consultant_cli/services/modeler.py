from __future__ import annotations

import hashlib
import json
import gzip
import re
from collections import Counter
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import Project, now_iso
from consultant_cli.infrastructure.store import RepositoryPaths, atomic_write_json


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("➜", "→").strip().casefold())


def _tokens(value: str, limit: int = 64) -> set[str]:
    stop = {
        "бизнес", "процесс", "создать", "сделать", "нужно", "через", "этого",
        "который", "пользователь", "инструкция", "схема", "режим",
    }
    counts = Counter(
        token
        for token in re.findall(r"[0-9a-zа-яё]+", value.casefold())
        if len(token) >= 4 and token not in stop
    )
    ranked = sorted(counts, key=lambda token: (-counts[token], -len(token), token))
    return set(ranked[:limit])


def _is_erp(value: str) -> bool:
    folded = value.casefold()
    return "erp" in folded or "управление предприятием" in folded


class ModelerReviewService:
    """Search every Modeler graph and independently review generated UI paths."""

    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.root = paths.root / "1c_modeler_upgrade"
        self.graphs = paths.modeler_graphs()
        self.manifest_path = self.graphs / "graph_manifest.json"
        self.route_graph_path = self.graphs / "1c_erp_2_5_route_graph.json"
        self.semantic_graph_path = self.graphs / "1c_erp_2_5_semantic_graph.json"
        self.search_index_path = self.graphs / "search-index.ndjson.gz"
        self.metadata_manifest_path = paths.root / "metadata" / "index" / "configuration.json"
        self._route_payload_cache: dict[str, Any] | None = None

    def _route_payload(self) -> dict[str, Any]:
        if self._route_payload_cache is None:
            try:
                self._route_payload_cache = json.loads(
                    self.route_graph_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError):
                self._route_payload_cache = {}
        return self._route_payload_cache

    @staticmethod
    def _route_segments(value: str) -> list[str]:
        return [segment.strip() for segment in value.split("→") if segment.strip()]

    @staticmethod
    def _route_words(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[0-9a-zа-яё]+", value.casefold())
            if len(token) >= 4 and token not in {"документ", "документы"}
        }

    @staticmethod
    def _route_overlap(left: set[str], right: set[str]) -> int:
        """Count exact or conservative Russian-inflection word matches."""
        return sum(
            1
            for word in left
            if any(
                word == candidate
                or (
                    len(word) >= 5
                    and len(candidate) >= 5
                    and word[:5] == candidate[:5]
                )
                for candidate in right
            )
        )

    def normalize_ui_paths(
        self, project: Project, result: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Replace shortened writer paths with exact routes from Modeler.

        The route graph is an exact-release derived metadata artifact.  A path
        is repaired only when the requested section and command/form yield a
        deterministic best route.  The selected route node is persisted in
        ``sources`` and linked to the step.
        """
        routes = self._route_payload()
        if not routes or str(routes.get("release") or "") != project.configuration.release:
            return []
        route_nodes = routes.get("nodes", {})
        values = route_nodes.values() if isinstance(route_nodes, dict) else route_nodes
        nodes = [node for node in values or [] if isinstance(node, dict)]
        sources = result.setdefault("sources", [])
        if not isinstance(sources, list):
            return []
        source_ids = {
            str(source.get("id") or "")
            for source in sources
            if isinstance(source, dict)
        }
        preferred_22 = any(
            str(document.get("node_id") or "").endswith("2_2")
            for branch in result.get("document_flow", []) or []
            if isinstance(branch, dict)
            for document in branch.get("documents", []) or []
            if isinstance(document, dict)
        )
        repairs: list[dict[str, str]] = []

        def source_for(node: dict[str, Any]) -> str:
            route_id = str(node.get("id") or "")
            source_id = f"modeler-route:{route_id}"
            if source_id not in source_ids:
                properties = node.get("properties", {}) or {}
                sources.append(
                    {
                        "id": source_id,
                        "title": f"Маршрут Modeler: {properties.get('path') or node.get('label')}",
                        "local_ref": "1c_modeler_upgrade/graphs/1c_erp_2_5_route_graph.json",
                        "url": "",
                        "product": project.configuration.product,
                        "release": project.configuration.release,
                        "verification_status": "verified_metadata",
                        "notes": (
                            "Точный узел производного route-графа Modeler; исходный "
                            f"XML-путь: {properties.get('source_path') or 'не указан'}."
                        ),
                        "source_ref": str(properties.get("source_path") or route_id),
                        "node_id": route_id,
                        "edge_ids": [],
                    }
                )
                source_ids.add(source_id)
            return source_id

        def best_route(
            requested_path: str,
            form: str,
            requested_label: str | None = None,
        ) -> dict[str, Any] | None:
            requested_segments = self._route_segments(requested_path)
            requested_first = (
                _normalized(requested_segments[0]) if requested_segments else ""
            )
            requested_last = requested_label or (
                requested_segments[-1] if requested_segments else ""
            )
            requested_words = self._route_words(requested_last)
            middle_words = self._route_words(" ".join(requested_segments[1:-1]))
            all_form_labels = [item.strip() for item in re.findall(r"«([^»]+)»", form)]
            label_scores = [
                self._route_overlap(self._route_words(item), requested_words)
                for item in all_form_labels
            ]
            best_label_score = max(label_scores, default=0)
            form_labels = [
                item
                for item, score in zip(all_form_labels, label_scores)
                if score == best_label_score
            ] if best_label_score else all_form_labels
            form_words = self._route_words(" ".join(form_labels))
            form_identity = form.casefold().strip()
            for russian_prefix, metadata_prefix in {
                "документ.": "document.",
                "справочник.": "catalog.",
                "отчет.": "report.",
                "обработка.": "dataprocessor.",
            }.items():
                if form_identity.startswith(russian_prefix):
                    form_identity = metadata_prefix + form_identity[len(russian_prefix):]
                    break
            technical_tail = form.split(".")[-1].casefold() if "." in form else ""
            ranked: list[tuple[int, int, str, dict[str, Any]]] = []
            for node in nodes:
                properties = node.get("properties", {}) or {}
                candidate_path = str(properties.get("path") or "")
                candidate_segments = self._route_segments(candidate_path)
                if not candidate_segments or "служебные подсистемы" in candidate_path.casefold():
                    continue
                candidate_first = _normalized(candidate_segments[0])
                candidate_label = str(node.get("label") or candidate_segments[-1])
                candidate_words = self._route_words(candidate_label)
                serialized = json.dumps(node, ensure_ascii=False).casefold()
                candidate_technical = str(properties.get("technical_name") or "").casefold()
                technical_parts = candidate_technical.split(".")
                candidate_object_name = (
                    technical_parts[1] if len(technical_parts) >= 2 else ""
                )
                human_object_names = {
                    re.sub(r"[^0-9a-zа-яё]+", "", item.casefold())
                    for item in form_labels
                }
                exact_object_match = bool(
                    form_identity
                    and candidate_technical.startswith(form_identity + ".")
                    or (
                        candidate_object_name
                        and candidate_object_name in human_object_names
                        and technical_parts[0] in {"document", "catalog"}
                    )
                )
                technical_match = bool(
                    exact_object_match
                    or (technical_tail and technical_tail in candidate_technical)
                )
                requested_overlap = self._route_overlap(
                    requested_words, candidate_words
                )
                form_overlap = self._route_overlap(form_words, candidate_words)
                overlap = self._route_overlap(
                    requested_words | form_words, candidate_words
                )
                exact_label_match = (
                    _normalized(candidate_label) == _normalized(requested_last)
                )
                if not technical_match and not exact_label_match and max(
                    requested_overlap, form_overlap
                ) < 2:
                    continue
                score = 0
                if candidate_first == requested_first:
                    score += 80
                elif {
                    candidate_first,
                    requested_first,
                } <= {"администрирование", "нси и администрирование"}:
                    score += 70
                if exact_label_match:
                    score += 120
                if any(
                    _normalized(candidate_label) == _normalized(form_label)
                    for form_label in form_labels
                ):
                    score += 150
                score += overlap * 30
                score += self._route_overlap(
                    middle_words, self._route_words(candidate_path)
                ) * 12
                if exact_object_match:
                    # A list command of the exact catalog/document is stronger
                    # than a textual label overlap.  This prevents similarly
                    # named reports from winning (for example, a report about
                    # internal consumption instead of the document list).
                    score += 180
                elif technical_match:
                    score += 45
                if preferred_22 and "заказ" in requested_last.casefold() and "производ" in requested_last.casefold():
                    if "2.2" in candidate_path or "2_2" in serialized:
                        score += 45
                if "регламент" in candidate_path.casefold() or "внеоборот" in candidate_path.casefold():
                    score -= 35
                if "базовая" in str(properties.get("source_path") or "").casefold():
                    # ERP contains duplicate routes inherited from basic
                    # subsystems.  Prefer the full ERP workplace route, e.g.
                    # ``Продажи → Оптовые продажи`` over
                    # ``Продажи → Ведение заказов клиентов``.
                    score -= 90
                if score < 45:
                    continue
                ranked.append((score, -len(candidate_segments), candidate_path, node))
            if not ranked:
                return None
            ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
            return ranked[0][3]

        for step in result.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            current = str(step.get("ui_path") or "").strip()
            form = str(step.get("form") or "")
            if not current or current.casefold().startswith("не применяется"):
                continue
            requested_segments = self._route_segments(current)
            labels: list[str] = []
            if requested_segments and "/" in requested_segments[-1]:
                labels = [item.strip() for item in requested_segments[-1].split("/") if item.strip()]
            selected_nodes: list[dict[str, Any]] = []
            declared_paths = [
                re.sub(r"^(?:затем|далее)\s+", "", item.strip(), flags=re.IGNORECASE)
                for item in current.split(";")
                if item.strip()
            ]
            if len(declared_paths) > 1:
                for declared_path in declared_paths:
                    selected = best_route(declared_path, form)
                    if selected is not None:
                        selected_nodes.append(selected)
                if len(selected_nodes) != len(declared_paths):
                    selected_nodes = []
            elif labels:
                prefix = " → ".join(requested_segments[:-1])
                for label in labels:
                    selected = best_route(prefix + " → " + label, form, label)
                    if selected is not None:
                        selected_nodes.append(selected)
            else:
                selected = best_route(current, form)
                if selected is not None:
                    selected_nodes.append(selected)
            if not selected_nodes or (labels and len(selected_nodes) != len(labels)):
                continue
            exact_paths = list(
                dict.fromkeys(
                    str(node.get("properties", {}).get("path") or "")
                    for node in selected_nodes
                )
            )
            exact_paths = [path for path in exact_paths if path]
            if not exact_paths:
                continue
            canonical = "; ".join(exact_paths)
            if canonical != current:
                step["ui_path"] = canonical
                repairs.append(
                    {
                        "step_id": str(step.get("id") or ""),
                        "from": current,
                        "to": canonical,
                    }
                )
            evidence_refs = step.setdefault("evidence_refs", [])
            if isinstance(evidence_refs, list):
                for node in selected_nodes:
                    source_id = source_for(node)
                    if source_id not in evidence_refs:
                        evidence_refs.append(source_id)
        return repairs

    def context(self, project: Project, query: str, per_graph: int = 4) -> str:
        """Return compact, ranked evidence candidates from all Modeler graphs."""
        if not self.search_index_path.exists():
            return (
                "MODELЕР НЕДОСТУПЕН: компактный индекс графов не построен. "
                "Не использовать графы как доказательство."
            )
        index_stat = self.search_index_path.stat()
        cache_key = hashlib.sha256(
            "\0".join(
                (
                    project.configuration.product,
                    project.configuration.release,
                    query,
                    str(per_graph),
                    str(index_stat.st_size),
                    str(index_stat.st_mtime_ns),
                )
            ).encode("utf-8")
        ).hexdigest()[:16]
        cache_path = (
            self.paths.results
            / project.project_id
            / "agent_artifacts"
            / f"modeler-context-{cache_key}.json"
        )
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached.get("context"), str):
                    return cached["context"]
            except (OSError, json.JSONDecodeError):
                pass
        tokens = _tokens(query)
        ranked: dict[str, list[tuple[int, dict[str, Any]]]] = {
            "route": [], "object": [], "semantic": [], "source": []
        }
        with gzip.open(self.search_index_path, mode="rt", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                graph = str(record.get("graph") or "")
                if graph not in ranked:
                    continue
                text = json.dumps(record, ensure_ascii=False).casefold()
                score = 0
                for token in tokens:
                    variants = {token}
                    if len(token) > 5 and re.fullmatch(r"[а-яё]+", token):
                        variants.add(token[:5])
                    score += max(min(text.count(variant), 8) for variant in variants)
                if not tokens or score <= 0:
                    continue
                ranked[graph].append((score, record))

        compatibility = self._compatibility(project)
        payload: dict[str, Any] = {
            "policy": (
                "Графы Modeler используются для поиска кандидатов. inferred требует "
                "ручной проверки; verified_metadata допустим только при совпадении "
                "точного локального XML релиза. Непроверяемые детали следует исключить."
            ),
            "project_configuration": project.configuration.product,
            "project_release": project.configuration.release,
            "compatibility": compatibility,
            "graphs": {},
        }
        for graph, items in ranked.items():
            selected = sorted(items, key=lambda item: (-item[0], str(item[1].get("id"))))[
                :per_graph
            ]
            payload["graphs"][graph] = [
                self._context_record(project, graph, record, score)
                for score, record in selected
            ]
        context = "Кандидаты из всех графов 1C Modeler:\n" + json.dumps(
            payload, ensure_ascii=False, indent=2
        )
        atomic_write_json(
            cache_path,
            {
                "schema_version": 1,
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "context": context,
            },
        )
        return context

    def _context_record(
        self, project: Project, graph: str, record: dict[str, Any], score: int
    ) -> dict[str, Any]:
        properties = dict(record.get("properties") or {})
        if properties.get("search_text"):
            properties["search_text"] = str(properties["search_text"])[:500]
        source_xml = str(properties.get("source_xml") or "")
        exact_xml = self._exact_xml_path(project, source_xml)
        if exact_xml:
            status = "verified_metadata"
            local_ref = exact_xml.relative_to(self.paths.root).as_posix()
        else:
            status = "inferred"
            candidate_ref = {
                "source": "1c_modeler_upgrade/1c_erp_2_5_source_graph.json",
                "object": "1c_modeler_upgrade/graphs/1c_erp_2_5_object_graph.json",
                "route": "1c_modeler_upgrade/graphs/1c_erp_2_5_route_graph.json",
                "semantic": "1c_modeler_upgrade/graphs/1c_erp_2_5_semantic_graph.json",
            }[graph]
            local_ref = candidate_ref if (self.paths.root / candidate_ref).is_file() else ""
        return {
            "score": score,
            "id": record.get("id"),
            "label": record.get("label"),
            "type": record.get("type"),
            "verification_status": status,
            "local_ref": local_ref,
            "configuration": record.get("configuration"),
            "release": record.get("release"),
            "properties": properties,
        }

    def _metadata_manifest(self) -> dict[str, Any]:
        if not self.metadata_manifest_path.exists():
            return {}
        try:
            return json.loads(self.metadata_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _compatibility(self, project: Project) -> str:
        manifest = self._metadata_manifest()
        if (
            _is_erp(project.configuration.product)
            and project.configuration.release
            and project.configuration.release == str(manifest.get("version") or "")
        ):
            return "exact_local_xml"
        if _is_erp(project.configuration.product):
            return "modeler_candidates_only"
        return "different_product"

    def _exact_xml_path(self, project: Project, source_xml: str) -> Path | None:
        if not source_xml or self._compatibility(project) != "exact_local_xml":
            return None
        source_root = str(self._metadata_manifest().get("source_root") or "")
        if not source_root:
            return None
        candidate = Path(source_root) / Path(source_xml.replace("/", "\\"))
        try:
            candidate.relative_to(self.paths.root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def review(self, project: Project, result: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = {
            "generated_at": now_iso(),
            "available": False,
            "verdict": "not_available",
            "configuration": project.configuration.product,
            "release": project.configuration.release,
            "modeler_configuration": "",
            "modeler_release": "",
            "compatibility": "not_checked",
            "semantic_graph_status": "missing",
            "summary": {
                "verified": 0,
                "verified_metadata": 0,
                "inferred": 0,
                "unresolved": 0,
                "semantic_verified_metadata": 0,
                "semantic_unresolved": 0,
            },
            "path_checks": [],
            "semantic_checks": [],
            "warnings": [],
        }
        if not self.manifest_path.exists() or not self.route_graph_path.exists():
            report["warnings"].append("Пакет 1C Modeler или граф маршрутов отсутствует.")
            return report

        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
            routes = self._route_payload()
        except (OSError, json.JSONDecodeError) as exc:
            report["verdict"] = "error"
            report["warnings"].append(f"Не удалось прочитать граф Modeler: {exc}")
            return report

        report["available"] = True
        modeler_configuration = str(
            routes.get("configuration") or manifest.get("configuration") or ""
        )
        modeler_release = str(routes.get("release") or "")
        report["modeler_configuration"] = modeler_configuration
        report["modeler_release"] = modeler_release
        product_matches = _is_erp(project.configuration.product) and _is_erp(
            modeler_configuration
        )
        release_matches = bool(modeler_release) and modeler_release == project.configuration.release
        if product_matches and release_matches:
            report["compatibility"] = "exact"
        elif product_matches:
            report["compatibility"] = "product_only"
            report["warnings"].append(
                "Релиз графа маршрутов Modeler не подтверждён или не совпадает; "
                "совпадения остаются inferred."
            )
        else:
            report["compatibility"] = "different_product"
            report["warnings"].append(
                "Конфигурация Modeler не соответствует конфигурации проекта."
            )

        semantic_edges: dict[str, dict[str, Any]] = {}
        semantic_release = ""
        if self.semantic_graph_path.exists():
            try:
                semantic = json.loads(self.semantic_graph_path.read_text(encoding="utf-8-sig"))
                report["semantic_graph_status"] = str(semantic.get("status") or "not_declared")
                semantic_release = str(semantic.get("release") or "")
                semantic_edges = {
                    str(edge.get("id")): edge
                    for edge in semantic.get("edges", [])
                    if isinstance(edge, dict) and edge.get("id")
                }
                if not semantic.get("edges"):
                    report["warnings"].append(
                        "Семантический граф Modeler не содержит связей между объектами."
                    )
            except (OSError, json.JSONDecodeError):
                report["semantic_graph_status"] = "error"

        route_nodes = routes.get("nodes", {})
        values = route_nodes.values() if isinstance(route_nodes, dict) else route_nodes
        by_path: dict[str, list[dict[str, Any]]] = {}
        for node in values or []:
            if not isinstance(node, dict):
                continue
            path = str(node.get("properties", {}).get("path") or "")
            if path:
                by_path.setdefault(_normalized(path), []).append(node)

        for step in result.get("steps", []):
            step_id = str(step.get("id") or "")
            ui_path = str(step.get("ui_path") or "").strip()
            check: dict[str, Any] = {
                "step_id": step_id,
                "ui_path": ui_path,
                "status": "unresolved",
                "matched_routes": [],
                "reason": "",
            }
            local_evidence = self._matching_local_evidence(project, step, result, ui_path)
            if ui_path.casefold().startswith("не применяется"):
                check["status"] = "verified"
                check["reason"] = "Контрольный шаг не выполняется в отдельной форме 1С."
            elif local_evidence:
                check["status"] = "verified_metadata"
                check["reason"] = "Путь найден в источнике точного локального релиза."
                check["local_evidence"] = local_evidence
            elif not ui_path or "не подтверж" in ui_path.casefold():
                check["reason"] = "Точный путь не заявлен в инструкции."
            else:
                declared_paths = [
                    item.strip() for item in ui_path.split(";") if item.strip()
                ]
                matches_by_path = [by_path.get(_normalized(item), []) for item in declared_paths]
                matches = [item for group in matches_by_path for item in group]
                check["matched_routes"] = [
                    {
                        "id": str(item.get("id") or ""),
                        "label": str(item.get("label") or ""),
                        "source_path": str(item.get("properties", {}).get("source_path") or ""),
                    }
                    for item in matches
                ]
                all_matched = bool(declared_paths) and all(matches_by_path)
                if all_matched and report["compatibility"] == "exact":
                    check["status"] = "verified_metadata"
                    check["reason"] = (
                        "Путь подтверждён точным узлом route-графа Modeler релиза проекта."
                    )
                elif matches:
                    check["status"] = "inferred"
                    check["reason"] = (
                        "Путь найден в Modeler, но точный релиз или доступный первичный "
                        "источник графа не подтверждён."
                    )
                else:
                    check["reason"] = "Точное совпадение пути в графе Modeler не найдено."
            report["summary"][check["status"]] += 1
            report["path_checks"].append(check)

            for relation_id in step.get("semantic_relation_refs", []) or []:
                relation_id = str(relation_id)
                edge = semantic_edges.get(relation_id)
                relation_check: dict[str, Any] = {
                    "step_id": step_id,
                    "relation_id": relation_id,
                    "status": "unresolved",
                    "relationship": "",
                    "source": "",
                    "target": "",
                    "source_ref": "",
                    "reason": "",
                }
                if edge is None:
                    relation_check["reason"] = "Ребро с таким id отсутствует в semantic graph."
                else:
                    relation_check.update(
                        {
                            "relationship": str(edge.get("relationship") or ""),
                            "source": str(edge.get("source") or ""),
                            "target": str(edge.get("target") or ""),
                            "source_ref": str(edge.get("source_ref") or ""),
                        }
                    )
                    source_path = self.paths.root / relation_check["source_ref"]
                    if (
                        report["compatibility"] == "exact"
                        and report["semantic_graph_status"] == "ГОТОВ"
                        and semantic_release == project.configuration.release
                        and edge.get("verification_status") == "verified_metadata"
                        and source_path.is_file()
                    ):
                        relation_check["status"] = "verified_metadata"
                        relation_check["reason"] = (
                            "Ребро и его первичный XML-источник подтверждены для точного релиза."
                        )
                    else:
                        relation_check["reason"] = (
                            "Не подтверждены готовность/релиз semantic graph либо первичный XML."
                        )
                if relation_check["status"] == "verified_metadata":
                    report["summary"]["semantic_verified_metadata"] += 1
                else:
                    report["summary"]["semantic_unresolved"] += 1
                report["semantic_checks"].append(relation_check)

        report["verdict"] = (
            "passed"
            if report["path_checks"]
            and report["summary"]["inferred"] == 0
            and report["summary"]["unresolved"] == 0
            and report["summary"]["semantic_unresolved"] == 0
            else "review_required"
        )
        return report

    def _matching_local_evidence(
        self,
        project: Project,
        step: dict[str, Any],
        result: dict[str, Any],
        ui_path: str,
    ) -> list[str]:
        if not ui_path or self._compatibility(project) != "exact_local_xml":
            return []
        sources = {
            str(item.get("id")): item
            for item in result.get("sources", [])
            if isinstance(item, dict)
        }
        matched: list[str] = []
        needle = _normalized(ui_path)
        for ref in step.get("evidence_refs", []):
            source = sources.get(str(ref), {})
            if source.get("verification_status") not in {"verified", "verified_metadata"}:
                continue
            local_ref = str(source.get("local_ref") or "")
            path = self.paths.root / local_ref
            if not local_ref or not path.is_file():
                continue
            # Graph bundles are large aggregations, not primary UI-path evidence.
            # Reading them once per step is both slow and less precise than the
            # route-node source_path check performed by review().
            if path.suffix.casefold() in {".json", ".gz"} or path.stat().st_size > 2_000_000:
                continue
            try:
                text = _normalized(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if needle and needle in text:
                matched.append(local_ref)
        return matched

    @staticmethod
    def markdown(report: dict[str, Any]) -> str:
        summary = report.get("summary", {})
        lines = [
            "# Независимая проверка 1C Modeler",
            "",
            f"- Результат: `{report.get('verdict', 'not_checked')}`",
            f"- Совместимость: `{report.get('compatibility', 'not_checked')}`",
            f"- Конфигурация Modeler: `{report.get('modeler_configuration') or 'не указана'}`",
            f"- Релиз Modeler: `{report.get('modeler_release') or 'не подтверждён'}`",
            f"- Семантический граф: `{report.get('semantic_graph_status', 'not_declared')}`",
            f"- Путей verified: {summary.get('verified', 0)}",
            f"- Путей verified_metadata: {summary.get('verified_metadata', 0)}",
            f"- Путей inferred: {summary.get('inferred', 0)}",
            f"- Неразрешённых путей: {summary.get('unresolved', 0)}",
            f"- Семантических связей verified_metadata: {summary.get('semantic_verified_metadata', 0)}",
            f"- Неразрешённых семантических связей: {summary.get('semantic_unresolved', 0)}",
            "",
            "> Неразрешённый путь блокирует утверждение инструкции.",
            "",
        ]
        if report.get("warnings"):
            lines.extend(["## Предупреждения", ""])
            lines.extend(f"- {item}" for item in report["warnings"])
            lines.append("")
        lines.extend(["## Проверка путей", "", "| Шаг | Путь | Статус | Причина |", "|---|---|---|---|"])
        for item in report.get("path_checks", []):
            path = str(item.get("ui_path") or "—").replace("|", "\\|")
            reason = str(item.get("reason") or "").replace("|", "\\|")
            lines.append(
                f"| {item.get('step_id', '')} | {path} | "
                f"`{item.get('status', 'unresolved')}` | {reason} |"
            )
        if report.get("semantic_checks"):
            lines.extend(
                [
                    "",
                    "## Проверка семантических связей",
                    "",
                    "| Шаг | Ребро | Связь | Источник → Цель | Статус | Причина |",
                    "|---|---|---|---|---|---|",
                ]
            )
            for item in report["semantic_checks"]:
                relation = str(item.get("relationship") or "—").replace("|", "\\|")
                endpoints = (
                    f"{item.get('source') or '—'} → {item.get('target') or '—'}"
                ).replace("|", "\\|")
                reason = str(item.get("reason") or "").replace("|", "\\|")
                lines.append(
                    f"| {item.get('step_id', '')} | {item.get('relation_id', '')} | "
                    f"{relation} | {endpoints} | `{item.get('status', 'unresolved')}` | "
                    f"{reason} |"
                )
        return "\n".join(lines) + "\n"
