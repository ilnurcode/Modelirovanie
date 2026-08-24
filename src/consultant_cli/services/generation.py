from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import Project, now_iso
from consultant_cli.errors import GenerationValidationError
from consultant_cli.infrastructure import yamlio
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.sources import SourceRoute


ALLOWED_VERIFICATION = {"verified", "verified_metadata", "inferred"}


class GenerationContract:
    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.schema_path = paths.root / "schemas" / "generation-result.schema.json"

    def schema(self) -> dict[str, Any]:
        return json.loads(self.schema_path.read_text(encoding="utf-8"))

    def validate(
        self,
        result: dict[str, Any],
        expected_type: str,
        project: Project,
        route: SourceRoute,
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
            for step in steps or []:
                if not isinstance(step, dict):
                    errors.append("Каждый шаг должен быть объектом")
                    continue
                status = str(step.get("verification_status") or "")
                if status not in ALLOWED_VERIFICATION:
                    errors.append(f"Недопустимый статус шага {step.get('id')}: {status}")
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
                if not route.use_xml and status == "verified_metadata":
                    errors.append(
                        f"Шаг {step.get('id')} помечен verified_metadata при несовместимом XML"
                    )
                if not isinstance(step.get("actions"), list) or not step.get("actions"):
                    errors.append(f"Шаг {step.get('id')} не содержит действий")
                relation_refs = step.get("semantic_relation_refs", [])
                if relation_refs is not None and not isinstance(relation_refs, list):
                    errors.append(
                        f"Шаг {step.get('id')} содержит некорректные semantic_relation_refs"
                    )
                if expected_type == "instruction":
                    for field, label in {
                        "ui_path": "путь в интерфейсе",
                        "result": "результат",
                        "verification": "проверку",
                    }.items():
                        if not str(step.get(field, "")).strip():
                            errors.append(f"Шаг {step.get('id')} не содержит {label}")

        if route.web_search_required and expected_type != "questions" and url_sources == 0:
            errors.append(
                "Для этой конфигурации требуется внешняя документация/веб-поиск, но URL отсутствуют"
            )
        if errors:
            raise GenerationValidationError("; ".join(errors))


class PromptBuilder:
    def __init__(self, paths: RepositoryPaths):
        self.paths = paths

    def build(
        self,
        project: Project,
        stage: str,
        user_prompt: str,
        route_context: str,
        existing_context: str = "",
        modeler_context: str = "",
    ) -> str:
        skill = {
            "questions": "skills/analyze-1c-requirements/SKILL.md",
            "design": "skills/design-1c-process/SKILL.md",
            "instruction": "skills/write-1c-user-instruction/SKILL.md",
        }[stage]
        skill_text = (self.paths.root / skill).read_text(encoding="utf-8")
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
        mode_rules = (
            "Полный режим: сначала обязательные уточняющие вопросы, затем явный апрув "
            "требований, проект и подробная схема, второй апрув, инструкция и итоговая проверка."
        )
        expected = {
            "questions": "Сформируйте только пакет обязательных уточняющих вопросов.",
            "design": "Сформируйте проект решения, конкретную Mermaid-схему и трассируемые шаги.",
            "instruction": (
                "Сформируйте полную, но не перегруженную пользовательскую инструкцию "
                "и схему. Не возвращайте пустые steps из-за наличия inferred в "
                "утверждённом проекте: создайте проверяемый черновик, сохранив таким "
                "шагам статус inferred. Не используйте unresolved и не повышайте "
                "статус без доказательства; inferred заблокирует только финальный "
                "апрув инструкции, но не её формирование."
            ),
        }[stage]
        return f"""Вы — консультант 1С, работающий по проверяемой базе знаний.

Верните ТОЛЬКО JSON-объект по переданной JSON Schema, без Markdown-обёртки.
artifact_type обязан быть: {stage}.

{mode_rules}
{expected}

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
- указывать только существующий файл, никогда не придумывать каталог, сокращённый
  псевдопуть или имя набора графов;
- не указывать `schema.json`, JSON Schema ответа, список файлов рабочей папки,
  служебные наблюдения агента и внутренние инструменты как источники предметного
  решения;
- каждый идентификатор в evidence_refs обязан дословно совпадать с id одного из
  объектов массива sources в том же ответе; не создавать скрытые или служебные
  идентификаторы источников;
- verified/verified_metadata требуют evidence_refs;
- допустимы только verified, verified_metadata и inferred;
- при отсутствии доказательства не создавать утверждение и не подменять пробел другим статусом;
- каждый шаг должен содержать evidence_refs, включая inferred;
- для заявленной связи между объектами указывать semantic_relation_refs с id
  соответствующих рёбер semantic graph; отсутствие ребра запрещает утверждать связь;
- ERP XML применять только если маршрут разрешает use_xml=true;
- для другой конфигурации сравнить подходящую документацию и разрешённые веб-источники;
- инструкция должна быть пошаговой, конкретной и без повторяющегося текста;
- в diagram_mermaid вернуть содержимое Mermaid без тройных обратных кавычек.

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
        return f"""# {prefix}: {data.get('title', project.title)}

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
