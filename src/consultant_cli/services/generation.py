from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import Project, now_iso
from consultant_cli.errors import GenerationValidationError
from consultant_cli.infrastructure import frontmatter, yamlio
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.sources import SourceRoute


ALLOWED_VERIFICATION = {"verified", "verified_metadata", "inferred"}


class GenerationContract:
    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.schema_path = paths.root / "schemas" / "generation-result.schema.json"

    def schema(self) -> dict[str, Any]:
        return json.loads(self.schema_path.read_text(encoding="utf-8"))

    def normalize_known_project_refs(
        self, result: dict[str, Any], project: Project
    ) -> list[dict[str, str]]:
        """Repair only deterministic aliases of project-owned source artifacts."""
        prefix = f"results/{project.project_id}/"
        aliases = {
            prefix + "request.md": prefix + "00-request.md",
            prefix + "tz.md": prefix + "00-source.md",
            prefix + "source.md": prefix + "00-source.md",
        }
        repairs: list[dict[str, str]] = []
        for source in result.get("sources", []):
            if not isinstance(source, dict):
                continue
            for field in ("local_ref", "source_ref"):
                current = str(source.get(field) or "").replace("\\", "/").lstrip("./")
                canonical = aliases.get(current)
                if canonical and (self.paths.root / canonical).is_file():
                    source[field] = canonical
                    repairs.append({"field": field, "from": current, "to": canonical})
        return repairs

    def normalize_missing_local_refs(self, result: dict[str, Any]) -> list[dict[str, str]]:
        """Normalize deterministic confusion between local_ref and url fields."""
        repairs: list[dict[str, str]] = []
        for source in result.get("sources", []) or []:
            if not isinstance(source, dict):
                continue
            local_ref = str(source.get("local_ref") or "").strip()
            url = str(source.get("url") or "").strip()
            if url and not url.startswith(("https://", "http://")):
                local_url = url.replace("\\", "/").lstrip("./")
                if (self.paths.root / local_url).is_file():
                    source["url"] = ""
                    if not local_ref:
                        source["local_ref"] = local_url
                    if str(source.get("source_ref") or "").strip() == url:
                        source["source_ref"] = local_ref or local_url
                    repairs.append(
                        {"field": "url", "from": url, "to": "", "reason": "local_ref"}
                    )
                    url = ""
            if (
                local_ref
                and not (self.paths.root / local_ref).is_file()
                and url.startswith(("https://", "http://"))
            ):
                source["local_ref"] = ""
                if str(source.get("source_ref") or "").strip() == local_ref:
                    source["source_ref"] = url
                repairs.append(
                    {"field": "local_ref", "from": local_ref, "to": url}
                )
        return repairs

    @staticmethod
    def normalize_known_verification_statuses(
        result: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Map the API's explicit ITS alias only when official evidence exists."""
        sources = {
            str(source.get("id") or ""): source
            for source in result.get("sources", []) or []
            if isinstance(source, dict)
        }
        repairs: list[dict[str, str]] = []
        for step in result.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            current = str(step.get("verification_status") or "")
            if current != "verified_its":
                continue
            official_refs = [
                str(reference)
                for reference in step.get("evidence_refs", []) or []
                if (
                    (source := sources.get(str(reference), {})).get(
                        "verification_status"
                    )
                    == "verified"
                    and str(source.get("url") or "").startswith(
                        ("https://", "http://")
                    )
                )
            ]
            canonical = "verified" if official_refs else "inferred"
            step["verification_status"] = canonical
            repairs.append(
                {
                    "step_id": str(step.get("id") or ""),
                    "from": current,
                    "to": canonical,
                    "evidence_refs": ",".join(official_refs),
                }
            )
        return repairs

    def normalize_required_official_url(
        self, result: dict[str, Any], route: SourceRoute
    ) -> list[dict[str, str]]:
        """Reuse an official URL already present in a routed local knowledge article.

        The role sometimes omits the URL field in a revision even though Python
        supplied the verified local article and its ``source_url`` in the prompt.
        Re-reading that exact routed file is deterministic and avoids a second API
        call; no URL is guessed and no network request is made here.
        """
        sources = result.get("sources")
        if not route.web_search_required or not isinstance(sources, list):
            return []
        if any(
            str(source.get("url") or "").startswith(("https://", "http://"))
            for source in sources
            if isinstance(source, dict)
        ):
            return []
        by_id = {
            str(source.get("id") or ""): source
            for source in sources
            if isinstance(source, dict)
        }
        for candidate in route.candidates:
            local_ref = str(candidate.ref or "").replace("\\", "/").lstrip("./")
            path = self.paths.root / local_ref
            if not path.is_file() or path.suffix.casefold() != ".md":
                continue
            metadata, _body = frontmatter.parse(path.read_text(encoding="utf-8"))
            url = str(metadata.get("source_url") or "").strip()
            if not url.startswith(("https://", "http://")):
                continue
            source_id = str(metadata.get("id") or local_ref)
            existing = by_id.get(source_id)
            if existing is not None:
                existing["url"] = url
                if not str(existing.get("source_ref") or "").strip():
                    existing["source_ref"] = url
                return [{"field": "url", "source_id": source_id, "to": url}]
            sources.append(
                {
                    "id": source_id,
                    "title": str(metadata.get("title") or candidate.title or path.stem),
                    "local_ref": local_ref,
                    "url": url,
                    "product": str(metadata.get("product") or route.requested_product),
                    "release": str(metadata.get("version") or route.requested_release),
                    "verification_status": "verified",
                    "notes": "Официальный URL восстановлен из выбранной локальной статьи.",
                    "source_ref": url,
                    "node_id": "",
                    "edge_ids": [],
                }
            )
            return [{"field": "source", "source_id": source_id, "to": url}]
        return []

    def normalize_unavailable_inferred_sources(
        self, result: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Remove inaccessible candidate sources without weakening verified evidence."""
        sources = result.get("sources")
        if not isinstance(sources, list):
            return []
        unavailable_ids: set[str] = set()
        repairs: list[dict[str, str]] = []
        retained = []
        for source in sources:
            if not isinstance(source, dict):
                retained.append(source)
                continue
            source_id = str(source.get("id") or "")
            local_ref = str(source.get("local_ref") or "").strip()
            url = str(source.get("url") or "").strip()
            unavailable = (
                source.get("verification_status") == "inferred"
                and bool(local_ref)
                and not (self.paths.root / local_ref).is_file()
                and not url.startswith(("https://", "http://"))
            )
            if unavailable:
                unavailable_ids.add(source_id)
                repairs.append(
                    {
                        "source_id": source_id,
                        "from": local_ref,
                        "to": "removed_unavailable_inferred_source",
                    }
                )
            else:
                retained.append(source)
        if not unavailable_ids:
            return []
        result["sources"] = retained
        for branch in result.get("document_flow", []) or []:
            if not isinstance(branch, dict):
                continue
            for document in branch.get("documents", []) or []:
                if isinstance(document, dict) and isinstance(
                    document.get("evidence_refs"), list
                ):
                    document["evidence_refs"] = [
                        ref
                        for ref in document["evidence_refs"]
                        if str(ref) not in unavailable_ids
                    ]
        for step in result.get("steps", []) or []:
            if isinstance(step, dict) and isinstance(step.get("evidence_refs"), list):
                step["evidence_refs"] = [
                    ref for ref in step["evidence_refs"] if str(ref) not in unavailable_ids
                ]
        return repairs

    @staticmethod
    def normalize_vanessa_ui_paths(
        result: dict[str, Any], path_repairs: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Apply the same exact Modeler route expansion to Gherkin text."""
        feature = str(result.get("vanessa_feature") or "")
        if not feature or not path_repairs:
            return []
        repairs: list[dict[str, str]] = []
        for item in sorted(
            path_repairs, key=lambda value: len(str(value.get("from") or "")), reverse=True
        ):
            old = str(item.get("from") or "")
            new = str(item.get("to") or "")
            if old and new and old in feature:
                feature = feature.replace(old, new)
                repairs.append({"from": old, "to": new})
        result["vanessa_feature"] = feature
        return repairs

    def normalize_incompatible_metadata_steps(
        self,
        result: dict[str, Any],
        project: Project,
        route: SourceRoute,
        *,
        require_modeler_route: bool = False,
    ) -> list[dict[str, str]]:
        """Downgrade metadata claims not backed by an exact-release graph source."""
        if route.use_xml:
            return []
        sources = {
            str(source.get("id") or ""): source
            for source in result.get("sources", []) or []
            if isinstance(source, dict)
        }
        repairs: list[dict[str, str]] = []
        for step in result.get("steps", []) or []:
            if not isinstance(step, dict) or step.get("verification_status") != "verified_metadata":
                continue
            exact_refs = []
            for ref in step.get("evidence_refs", []) or []:
                source = sources.get(str(ref), {})
                local_ref = str(source.get("local_ref") or "")
                source_id = str(source.get("id") or "")
                if (
                    source.get("verification_status") == "verified_metadata"
                    and str(source.get("release") or "") == project.configuration.release
                    and local_ref.startswith("1c_modeler_upgrade/graphs/")
                    and (self.paths.root / local_ref).is_file()
                    and (not require_modeler_route or source_id.startswith("modeler-route:"))
                ):
                    exact_refs.append(source_id)
            if not exact_refs:
                step["verification_status"] = "inferred"
                repairs.append(
                    {
                        "step_id": str(step.get("id") or ""),
                        "from": "verified_metadata",
                        "to": "inferred",
                    }
                )
        return repairs

    def normalize_evidence_refs(self, result: dict[str, Any]) -> list[dict[str, str]]:
        """Repair unambiguous aliases of source IDs without inventing evidence.

        Models occasionally put a source ``node_id`` or ``source_ref`` into an
        ``evidence_refs`` array even though the contract requires ``sources[].id``.
        Those values describe the same source and can be mapped deterministically.
        Unknown or ambiguous references are intentionally left untouched so the
        validator still blocks unsupported claims.
        """
        sources = result.get("sources", [])
        if not isinstance(sources, list):
            return []

        alias_candidates: dict[str, set[str]] = {}
        title_candidates: dict[str, set[str]] = {}
        source_ids: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("id") or "").strip()
            if not source_id:
                continue
            source_ids.add(source_id)
            for field in ("id", "node_id", "source_ref", "local_ref"):
                alias = str(source.get(field) or "").strip()
                if alias:
                    alias_candidates.setdefault(alias.casefold(), set()).add(source_id)
            title = " ".join(str(source.get("title") or "").casefold().split())
            if title:
                title_candidates.setdefault(title, set()).add(source_id)

        aliases = {
            alias: next(iter(candidates))
            for alias, candidates in alias_candidates.items()
            if len(candidates) == 1
        }
        titles = {
            title: next(iter(candidates))
            for title, candidates in title_candidates.items()
            if len(candidates) == 1
        }
        repairs: list[dict[str, str]] = []

        def normalize_refs(owner: dict[str, Any], owner_label: str) -> None:
            references = owner.get("evidence_refs")
            if not isinstance(references, list):
                return
            for index, reference in enumerate(references):
                current = str(reference).strip()
                if current in source_ids:
                    continue
                canonical = aliases.get(current.casefold())
                if canonical:
                    references[index] = canonical
                    repairs.append(
                        {
                            "owner": owner_label,
                            "from": current,
                            "to": canonical,
                        }
                    )

        for branch in result.get("document_flow", []) or []:
            if not isinstance(branch, dict):
                continue
            for document in branch.get("documents", []) or []:
                if not isinstance(document, dict):
                    continue
                label = f"document:{str(document.get('name') or '').strip()}"
                normalize_refs(document, label)
                references = document.get("evidence_refs")
                if isinstance(references, list) and not references:
                    node_id = str(document.get("node_id") or "").strip()
                    name = " ".join(str(document.get("name") or "").casefold().split())
                    canonical = aliases.get(node_id.casefold()) if node_id else None
                    canonical = canonical or titles.get(name)
                    if canonical:
                        references.append(canonical)
                        repairs.append({"owner": label, "from": "", "to": canonical})

        for step in result.get("steps", []) or []:
            if isinstance(step, dict):
                normalize_refs(step, f"step:{str(step.get('id') or '').strip()}")
        return repairs

    def validate(
        self,
        result: dict[str, Any],
        expected_type: str,
        project: Project,
        route: SourceRoute,
        *,
        allow_empty_document_flow: bool = False,
    ) -> None:
        errors: list[str] = []
        if result.get("artifact_type") != expected_type:
            errors.append(
                f"artifact_type должен быть {expected_type}, получено {result.get('artifact_type')!r}"
            )
        if not str(result.get("title", "")).strip():
            errors.append("Не заполнен title")
        if not isinstance(result.get("sources"), list):
            errors.append("sources должен быть массивом")
        sources = result.get("sources", []) if isinstance(result.get("sources"), list) else []
        source_ids: set[str] = set()
        url_sources = 0
        for source in sources:
            if not isinstance(source, dict):
                errors.append("Каждый источник должен быть объектом")
                continue
            source_id = str(source.get("id", "")).strip()
            if not source_id:
                errors.append("У источника отсутствует id")
            elif source_id in source_ids:
                errors.append(f"Повторяется id источника: {source_id}")
            source_ids.add(source_id)
            status = str(source.get("verification_status") or "")
            if status not in ALLOWED_VERIFICATION:
                errors.append(f"Недопустимый статус источника {source_id}: {status}")
            local_ref = str(source.get("local_ref", "")).strip()
            if local_ref and not (self.paths.root / local_ref).exists():
                errors.append(f"Локальная ссылка не существует: {local_ref}")
            url = str(source.get("url", "")).strip()
            if url:
                if not url.startswith(("https://", "http://")):
                    errors.append(f"Некорректный URL источника {source_id}")
                else:
                    url_sources += 1

        if expected_type == "questions":
            questions = result.get("questions")
            if not isinstance(questions, list) or not questions:
                errors.append("Полный режим должен вернуть обязательные вопросы")
        else:
            steps = result.get("steps")
            if not isinstance(steps, list) or not steps:
                errors.append("Проект/инструкция должны содержать steps")
            if project.generation.diagram and not str(result.get("diagram_mermaid", "")).strip():
                errors.append("Включена схема, но diagram_mermaid отсутствует")
            document_flow = result.get("document_flow")
            if not isinstance(document_flow, list) or (
                not document_flow and not allow_empty_document_flow
            ):
                errors.append("Проект/инструкция должны начинаться с document_flow")
            for branch in document_flow or []:
                if not isinstance(branch, dict) or not branch.get("documents"):
                    errors.append("Каждая ветка document_flow должна содержать documents")
                    continue
                for document in branch.get("documents", []):
                    references = document.get("evidence_refs", []) if isinstance(document, dict) else []
                    if not references or any(reference not in source_ids for reference in references):
                        errors.append(
                            f"Документ {document.get('name', '') if isinstance(document, dict) else ''} "
                            "не имеет допустимого evidence_refs"
                        )
            if (
                expected_type == "instruction"
                and project.generation.deliverable == "vanessa"
                and not str(result.get("vanessa_feature", "")).strip()
            ):
                errors.append("Для режима vanessa обязательно поле vanessa_feature")
            step_ids: set[str] = set()
            for step in steps or []:
                if not isinstance(step, dict):
                    errors.append("Каждый шаг должен быть объектом")
                    continue
                status = str(step.get("verification_status") or "")
                if status not in ALLOWED_VERIFICATION:
                    errors.append(f"Недопустимый статус шага {step.get('id')}: {status}")
                if expected_type == "instruction" and status == "inferred":
                    errors.append(
                        f"Шаг {step.get('id')} имеет inferred: вынесите неподтверждённый механизм в GAP"
                    )
                step_id = str(step.get("id") or "").strip()
                if not step_id:
                    errors.append("Шаг без id")
                elif step_id in step_ids:
                    errors.append(f"Повторяется id шага: {step_id}")
                step_ids.add(step_id)
                evidence = step.get("evidence_refs", [])
                if not evidence:
                    errors.append(
                        f"Шаг {step.get('id')} не содержит evidence_refs; "
                        "непроверяемые шаги запрещены"
                    )
                missing = [ref for ref in evidence if ref not in source_ids]
                if missing:
                    errors.append(
                        f"Шаг {step.get('id')} ссылается на неизвестные источники: {missing}"
                    )
                exact_graph_metadata = any(
                    (
                        (source := next(
                            (
                                item
                                for item in sources
                                if isinstance(item, dict)
                                and str(item.get("id") or "") == str(ref)
                            ),
                            {},
                        )).get("verification_status") == "verified_metadata"
                        and str(source.get("release") or "") == project.configuration.release
                        and str(source.get("local_ref") or "").startswith(
                            "1c_modeler_upgrade/graphs/"
                        )
                    )
                    for ref in evidence
                )
                if not route.use_xml and status == "verified_metadata" and not exact_graph_metadata:
                    errors.append(
                        f"Шаг {step.get('id')} помечен verified_metadata при несовместимом XML"
                    )
                if not isinstance(step.get("actions"), list) or not step.get("actions"):
                    errors.append(f"Шаг {step.get('id')} не содержит действий")
                action_fingerprints: set[str] = set()
                for action in step.get("actions") or []:
                    normalized = " ".join(str(action).casefold().split())
                    if any(
                        phrase in normalized
                        for phrase in (
                            "выполнить подтверждённый шаг",
                            "выполнить подтвержденный шаг",
                            "обеспечить выполнение требования",
                            "реализовать требование",
                        )
                    ):
                        errors.append(
                            f"Шаг {step.get('id')} пересказывает требование вместо операции 1С"
                        )
                    if normalized in action_fingerprints:
                        errors.append(
                            f"В шаге {step.get('id')} повторяется одно действие: {action}"
                        )
                    action_fingerprints.add(normalized)
                relation_refs = step.get("semantic_relation_refs", [])
                if relation_refs is not None and not isinstance(relation_refs, list):
                    errors.append(
                        f"Шаг {step.get('id')} содержит некорректные semantic_relation_refs"
                    )
                if expected_type == "instruction" and project.generation.deliverable in {
                    "hybrid",
                    "consultant",
                    "vanessa",
                }:
                    for field, label in {
                        "ui_path": "путь в интерфейсе",
                        "result": "результат",
                        "verification": "проверку",
                    }.items():
                        if not str(step.get(field, "")).strip():
                            errors.append(f"Шаг {step.get('id')} не содержит {label}")
                    if str(step.get("ui_path", "")).strip().casefold() in {
                        "не подтверждён.", "не подтвержден.", "не подтверждён", "не подтвержден"
                    }:
                        errors.append(f"Шаг {step.get('id')} содержит неподтверждённый UI-путь")

        if route.web_search_required and expected_type != "questions" and url_sources == 0:
            errors.append(
                "Для этой конфигурации требуется внешняя документация/веб-поиск, но URL отсутствуют"
            )
        if errors:
            raise GenerationValidationError("; ".join(errors))


class PromptBuilder:
    SKILL_BY_STAGE = {
        "questions": "skills/analyze-1c-requirements/SKILL.md",
        "design": "skills/design-1c-process/SKILL.md",
        "instruction": "skills/write-1c-user-instruction/SKILL.md",
    }
    SKILL_BY_ROLE = {
        "erp-translator": "skills/analyze-1c-requirements/SKILL.md",
        "erp-process-planner": "skills/design-1c-process/SKILL.md",
        "instruction-writer": "skills/write-1c-user-instruction/SKILL.md",
    }
    ORCHESTRATOR_SKILL = "skills/prepare-1c-consulting-answer/SKILL.md"

    def __init__(self, paths: RepositoryPaths):
        self.paths = paths

    def runtime_plan(self) -> dict[str, Any]:
        return {
            "execution": "python_prompt_composition",
            "orchestrator": {
                "path": self.ORCHESTRATOR_SKILL,
                "available": (self.paths.root / self.ORCHESTRATOR_SKILL).is_file(),
            },
            "roles": {
                role: {
                    "skill": skill,
                    "available": (self.paths.root / skill).is_file(),
                }
                for role, skill in self.SKILL_BY_ROLE.items()
            },
        }

    def build(
        self,
        project: Project,
        stage: str,
        user_prompt: str,
        route_context: str,
        existing_context: str = "",
        modeler_context: str = "",
        graph_context: str = "",
        role: str = "",
    ) -> str:
        skill = self.SKILL_BY_ROLE.get(role, self.SKILL_BY_STAGE[stage])
        skill_text = (self.paths.root / skill).read_text(encoding="utf-8")
        orchestrator_path = self.paths.root / self.ORCHESTRATOR_SKILL
        orchestrator_skill = (
            orchestrator_path.read_text(encoding="utf-8")
            if orchestrator_path.is_file()
            else ""
        )
        policy_path = (
            self.paths.root
            / "skills"
            / "prepare-1c-consulting-answer"
            / "references"
            / "modeling-policy.md"
        )
        modeling_policy = (
            policy_path.read_text(encoding="utf-8") if policy_path.exists() else ""
        )
        modeler_skill_path = self.paths.root / "1c_modeler_upgrade" / "SKILL.md"
        modeler_skill = (
            modeler_skill_path.read_text(encoding="utf-8")
            if modeler_skill_path.exists()
            else ""
        )
        role_prompt_path = self.paths.root / "prompts" / "roles" / f"{role}.md"
        role_prompt = (
            role_prompt_path.read_text(encoding="utf-8")
            if role_prompt_path.is_file()
            else ""
        )
        mode_rules = (
            "Полный режим: сначала обязательные уточняющие вопросы, затем явный апрув "
            "требований, проект и подробная схема, второй апрув, инструкция и итоговая проверка."
        )
        expected = {
            "questions": "Сформируйте только пакет обязательных уточняющих вопросов.",
            "design": "Сформируйте проект решения, конкретную Mermaid-схему и трассируемые шаги.",
            "instruction": (
                "Сформируйте полную, но не перегруженную пользовательскую инструкцию "
                "и схему из утверждённой solution model. Один шаг — одна реальная "
                "операция пользователя в 1С и может покрывать несколько требований. "
                "Не превращайте предложения ТЗ в шаги и не включайте inferred/candidate/"
                "unresolved как команды пользователю: вынесите их в limitations или GAP."
            ),
        }[stage]
        deliverable_rules = {
            "hybrid": (
                "Сформируйте единый результат: сначала полный сквозной процесс со всеми "
                "основными, параллельными, альтернативными и возвратными ветвями, затем "
                "подробную пошаговую инструкцию консультанта с предварительными настройками, "
                "полными UI-путями, полями, командами и проверками. Ни одна из двух частей "
                "не является сокращённым приложением к другой."
            ),
            "process": (
                "Опишите сквозной процесс, ветви, роли, документы и контроль. UI-пути "
                "указывайте только когда они подтверждены."
            ),
            "consultant": (
                "Дайте инструкцию консультанта: предварительные настройки, затем куда "
                "перейти, что нажать, заполнить, провести и как проверить результат."
            ),
            "vanessa": (
                "Помимо структурированных шагов подготовьте исполнимый черновик Vanessa "
                "Automation в поле vanessa_feature; неподтверждённые селекторы не выдумывайте."
            ),
        }[project.generation.deliverable]
        return f"""Вы — консультант 1С, работающий по проверяемой базе знаний.

Верните ТОЛЬКО JSON-объект по переданной JSON Schema, без Markdown-обёртки.
artifact_type обязан быть: {stage}.
Внешняя роль: {role}. Модель выбирает приложение, а не интерфейс Codex/OpenCode/Pi.

{mode_rules}
{expected}
Формат результата: {project.generation.deliverable}. {deliverable_rules}

Конфигурация: {project.configuration.product}
Редакция: {project.configuration.edition}
Релиз: {project.configuration.release}
Уровень детализации: {project.generation.detail_level}
Схема включена: {project.generation.diagram}
Политика интернет-источников: {project.sources.internet_policy}

Правила доказательности:
- не выдумывать разделы, кнопки, флажки, формы, команды и ограничения;
- local_ref всегда указывать относительно корня репозитория, а не папки проекта;
- для артефактов этого проекта использовать префикс results/{project.project_id}/,
  например results/{project.project_id}/01-requirements.md;
- исходное ТЗ этого проекта указывать только как results/{project.project_id}/00-request.md
  или, для точной загруженной копии, results/{project.project_id}/00-source.md;
- указывать только существующий файл, никогда не придумывать каталог, сокращённый
  псевдопуть или имя набора графов;
- не указывать `schema.json`, JSON Schema ответа, список файлов рабочей папки,
  служебные наблюдения агента и внутренние инструменты как источники предметного
  решения;
- каждый идентификатор в evidence_refs обязан дословно совпадать с id одного из
  объектов массива sources в том же ответе; не создавать скрытые или служебные
  идентификаторы источников;
- node_id и source_ref хранить в одноимённых полях sources, но не подставлять их
  вместо sources[].id в evidence_refs; у каждого документа document_flow должна
  быть соответствующая запись sources;
- verified/verified_metadata требуют evidence_refs;
- допустимы только verified, verified_metadata и inferred;
- при отсутствии доказательства не создавать утверждение и не подменять пробел другим статусом;
- каждый шаг должен содержать evidence_refs, включая inferred;
- для заявленной связи между объектами указывать semantic_relation_refs с id
  соответствующих рёбер semantic graph; отсутствие ребра запрещает утверждать связь;
- ERP XML применять только если маршрут разрешает use_xml=true;
- для другой конфигурации сравнить подходящую документацию и разрешённые веб-источники;
- инструкция должна быть пошаговой, конкретной и без повторяющегося текста внутри
  одного шага; одинаковая реальная команда (например, «Провести документ») допустима
  в разных шагах для разных документов;
- действие шага должно начинаться с конкретного глагола и описывать фактическую
  операцию в форме 1С, а не фразы «выполнить требование»/«обеспечить процесс»;
- объединять требования, реализуемые одной операцией; число шагов определяется
  пользовательским маршрутом, а не числом атомарных требований;
- ui_path указывать полным маршрутом из Modeler со всеми промежуточными разделами;
  запрещено сокращать `Продажи → Оптовые продажи → Заказы клиентов` до
  `Продажи → Заказы клиентов` или пересказывать путь своими словами;
- для instruction запрещён статус inferred; неподтверждённый механизм описывать
  только в limitations/customizations с критерием проверки;
- в diagram_mermaid вернуть содержимое Mermaid без тройных обратных кавычек.
- для design/instruction первым смысловым блоком заполнить document_flow: основной
  сквозной маршрут, затем параллельные и альтернативные ветви; только документы,
  подтверждённые evidence_refs;
- source_ref и точные node/edge id из четырёхслойного графа сохранять в sources,
  evidence_refs и semantic_relation_refs, не заменять поисковым рангом;
{('- перед формированием вопросов проверить все смысловые кластеры; не останавливаться на шаблонных пяти вопросах; вернуть от 1 до 12 вопросов ровно по числу материальных бизнес-неопределённостей; в impact каждого вопроса перечислить точные REQ-id, а в summary указать, какие кластеры проверены и почему дополнительных вопросов не требуется;' if stage == 'questions' else '')}

Контракт внешней роли:
{role_prompt[:9000]}

Координирующий Python-скилл полного lifecycle:
{orchestrator_skill[:10000]}

Профильный скилл:
{skill_text[:12000]}

Общая политика моделирования:
{modeling_policy[:6000]}

Правила независимой проверки 1C Modeler:
{modeler_skill[:6000]}

Исходный запрос:
{user_prompt}

Маршрут и найденный локальный контекст:
{route_context}

Релевантные узлы графов Modeler:
{modeler_context[:18000]}

Четырёхслойный ERP-граф Яны (Python hybrid search + typed expansion):
{graph_context[:42000]}

Уже утверждённый или сохранённый контекст:
{existing_context[:16000]}
"""


class ArtifactRenderer:
    @staticmethod
    def _frontmatter(metadata: dict[str, Any], body: str) -> str:
        return f"---\n{yamlio.dumps(metadata)}---\n\n{body.strip()}\n"

    @staticmethod
    def _bullets(values: list[str] | None, empty: str = "Не указано.") -> str:
        values = [str(value).strip() for value in (values or []) if str(value).strip()]
        return "\n".join(f"- {value}" for value in values) if values else empty

    @staticmethod
    def _sources(values: list[dict[str, Any]] | None) -> str:
        rows = []
        for source in values or []:
            local_ref = str(source.get("local_ref") or "").lstrip("./")
            target = source.get("url") or (f"../../{local_ref}" if local_ref else "")
            label = source.get("title") or source.get("id") or "Источник"
            link = f"[{label}]({target})" if target else str(label)
            rows.append(
                f"- `{source.get('id', '')}` — {link}; "
                f"статус: `{source.get('verification_status', 'unresolved')}`. "
                f"{source.get('notes', '')}".rstrip()
            )
        return "\n".join(rows) if rows else "Источники не указаны."

    @staticmethod
    def _document_flow(values: list[dict[str, Any]] | None) -> str:
        blocks = []
        for branch in values or []:
            title = str(branch.get("title") or "Основной маршрут")
            condition = str(branch.get("condition") or "").strip()
            documents = [
                str(item.get("name") or "").strip()
                for item in branch.get("documents", [])
                if str(item.get("name") or "").strip()
            ]
            if not documents:
                continue
            heading = f"**{title}**" + (f" — {condition}" if condition else "")
            chain = "\n".join(
                f"{'   ' * index}{'→ ' if index else ''}{document}"
                for index, document in enumerate(documents)
            )
            blocks.append(f"{heading}:\n\n{chain}")
        return "\n\n".join(blocks) if blocks else "Точная цепочка документов не подтверждена."

    def requirements(self, project: Project, data: dict[str, Any]) -> str:
        questions = []
        for question in data.get("questions", []):
            required = "обязательный" if question.get("required", True) else "необязательный"
            questions.append(
                f"### {question.get('id')}. {question.get('text')}\n\n"
                f"Статус: **{required}**.\n\n"
                f"Влияние: {question.get('impact', '')}\n\n"
                + (
                    "Варианты:\n\n"
                    + "\n".join(f"- {item}" for item in question.get("options", []))
                    + "\n"
                    if question.get("options")
                    else ""
                )
            )
        body = f"""# Вопросы и требования: {data.get('title', project.title)}

## Исходная цель

{data.get('summary', '')}

## Вопросы на согласование

{''.join(questions)}

## Источники

{self._sources(data.get('sources'))}

## Решение по апруву

Статус: `pending_approval`. До заполнения обязательных ответов и явного апрува проект решения не создаётся.
"""
        return self._frontmatter(
            {
                "artifact": "requirements",
                "process_id": project.project_id,
                "version": project.requirements_version + 1,
                "approval_status": "pending_approval",
                "approved_by": "",
                "approved_at": "",
                "approval_evidence": "",
            },
            body,
        )

    def design(self, project: Project, data: dict[str, Any]) -> str:
        return self._frontmatter(
            {
                "artifact": "design",
                "process_id": project.project_id,
                "requirements_version": project.requirements_version,
                "version": project.design_version + 1,
                "approval_status": "pending_approval",
                "approved_by": "",
                "approved_at": "",
                "approval_evidence": "",
            },
            self._common_body(project, data, "Проект решения и схема")
            + "\n\n## Решение по апруву\n\nСтатус: `pending_approval`. До явного апрува инструкцию не создавать.\n",
        )

    def instruction(self, project: Project, data: dict[str, Any]) -> str:
        metadata = {
            "artifact": "instruction",
            "process_id": project.project_id,
            "mode": project.mode.value,
            "version": project.instruction_version + 1,
            "status": "draft",
            "approval_status": "pending_approval",
            "review_status": "feedback_pending",
            "approved_by": "",
            "approved_at": "",
            "approval_evidence": "",
            "configuration": project.configuration.product,
            "release": project.configuration.release,
        }
        body = self._common_body(project, data, "Инструкция и схема")
        body += (
            "\n\n## Подтверждение консультанта\n\n"
            "Всё ли вас устраивает? Инструкция станет успешным примером только после явного подтверждения.\n"
        )
        return self._frontmatter(metadata, body)

    def _common_body(self, project: Project, data: dict[str, Any], prefix: str) -> str:
        steps = []
        for number, step in enumerate(data.get("steps", []), 1):
            step_id = step.get("id") or f"P{number:02d}"
            role = step.get("role", "не указана")
            actions = "\n".join(
                f"{index}. {action}" for index, action in enumerate(step.get("actions", []), 1)
            ) or "Действия не указаны."
            refs = ", ".join(f"`{ref}`" for ref in step.get("evidence_refs", [])) or "нет"
            semantic_refs = ", ".join(
                f"`{ref}`" for ref in step.get("semantic_relation_refs", [])
            ) or "не применяются"
            fields = self._bullets(step.get("fields"), "Не применяются.")
            steps.append(
                f"### {step_id}. {step.get('title', '')}\n\n"
                f"<!-- step_id: {step_id}; roles: [{role}]; evidence: "
                f"[{', '.join(step.get('evidence_refs', []))}] -->\n\n"
                f"**Роль:** {role}. **Предусловие:** {step.get('precondition') or 'Не указано.'}\n\n"
                f"**Путь:** {step.get('ui_path') or 'Не подтверждён.'} "
                f"**Форма:** {step.get('form') or 'Не указана.'}\n\n"
                f"{actions}\n\n"
                f"**Поля:**\n\n{fields}\n\n"
                f"**Команда:** {step.get('command') or 'Не указана.'} "
                f"**Ожидаемый статус:** {step.get('expected_status') or 'Не указан.'}\n\n"
                f"**Результат:** {step.get('result', '')}\n\n"
                f"**Проверка:** {step.get('verification') or 'Не указана.'}\n\n"
                f"**Источник:** {refs}. Статус доказательства: "
                f"`{step.get('verification_status', 'unresolved')}`. "
                f"Семантические связи: {semantic_refs}.\n"
            )
        diagram = str(data.get("diagram_mermaid", "")).strip()
        diagram_block = f"```mermaid\n{diagram}\n```" if project.generation.diagram and diagram else "Схема отключена."
        vanessa = str(data.get("vanessa_feature") or "").strip()
        vanessa_block = (
            f"\n\n## Сценарий Vanessa Automation\n\n```gherkin\n{vanessa}\n```"
            if project.generation.deliverable == "vanessa" and vanessa
            else ""
        )
        return f"""# {prefix}: {data.get('title', project.title)}

## Общая последовательность документов

{self._document_flow(data.get('document_flow'))}

## Краткий результат

{data.get('summary', '')}

## Предлагаемая реализация

{self._bullets(data.get('implementation'))}

## Роли

{self._bullets(data.get('roles'))}

## Механизмы и объекты

{self._bullets(data.get('objects'))}

## Необходимые настройки

{self._bullets(data.get('settings'))}

## Схема бизнес-процесса

{diagram_block}

## Пошаговый сценарий

{''.join(steps)}
{vanessa_block}

## Альтернативные варианты

{self._bullets(data.get('alternatives'))}

## Ограничения типового функционала

{self._bullets(data.get('limitations'))}

## Возможные доработки

{self._bullets(data.get('customizations'))}

## Источники

{self._sources(data.get('sources'))}
"""


def evidence_ndjson(data: dict[str, Any]) -> str:
    lines = []
    for source in data.get("sources", []):
        record = dict(source)
        record.setdefault("accessed_at", now_iso())
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + ("\n" if lines else "")


def instruction_validation_markdown(data: dict[str, Any]) -> str:
    steps = data.get("steps", [])
    inferred = [
        str(step.get("id", ""))
        for step in steps
        if step.get("verification_status") == "inferred"
    ]
    missing_ui = [str(step.get("id", "")) for step in steps if not step.get("ui_path")]
    verdict = "review_required" if inferred or missing_ui else "contract_passed"
    return (
        "# Проверка инструкции\n\n"
        f"- Результат: `{verdict}`\n"
        f"- Шагов: {len(steps)}\n"
        f"- Выведенные шаги: {', '.join(inferred) or 'нет'}\n"
        f"- Шаги без пути интерфейса: {', '.join(missing_ui) or 'нет'}\n"
        "- Контракт JSON, ссылки доказательств и совместимость XML проверены приложением.\n"
    )
