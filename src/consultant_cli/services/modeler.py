from __future__ import annotations

import json
import gzip
import re
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import Project, now_iso
from consultant_cli.infrastructure.store import RepositoryPaths


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("➜", "→").strip().casefold())


def _tokens(value: str) -> set[str]:
    stop = {
        "бизнес", "процесс", "создать", "сделать", "нужно", "через", "этого",
        "который", "пользователь", "инструкция", "схема", "режим",
    }
    return {
        token
        for token in re.findall(r"[0-9a-zа-яё]+", value.casefold())
        if len(token) >= 4 and token not in stop
    }


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

    def context(self, project: Project, query: str, per_graph: int = 4) -> str:
        """Return compact, ranked evidence candidates from all Modeler graphs."""
        if not self.search_index_path.exists():
            return (
                "MODELЕР НЕДОСТУПЕН: компактный индекс графов не построен. "
                "Не использовать графы как доказательство."
            )
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
        return "Кандидаты из всех графов 1C Modeler:\n" + json.dumps(
            payload, ensure_ascii=False, indent=2
        )

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
            local_ref = {
                "source": "1c_modeler_upgrade/1c_erp_2_5_source_graph.json",
                "object": "1c_modeler_upgrade/graphs/1c_erp_2_5_object_graph.json",
                "route": "1c_modeler_upgrade/graphs/1c_erp_2_5_route_graph.json",
                "semantic": "1c_modeler_upgrade/graphs/1c_erp_2_5_semantic_graph.json",
            }[graph]
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
            routes = json.loads(self.route_graph_path.read_text(encoding="utf-8-sig"))
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
                matches = by_path.get(_normalized(ui_path), [])
                check["matched_routes"] = [
                    {
                        "id": str(item.get("id") or ""),
                        "label": str(item.get("label") or ""),
                        "source_path": str(item.get("properties", {}).get("source_path") or ""),
                    }
                    for item in matches
                ]
                accessible = any(
                    (
                        Path(item["source_path"]).exists()
                        or (self.paths.root / item["source_path"]).exists()
                    )
                    for item in check["matched_routes"]
                    if item["source_path"]
                )
                if matches and report["compatibility"] == "exact" and accessible:
                    check["status"] = "verified"
                    check["reason"] = "Путь и первичный источник совпали с точным релизом."
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
