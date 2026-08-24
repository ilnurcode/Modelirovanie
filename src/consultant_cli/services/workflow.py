from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import (
    ConfigurationInfo,
    GenerationSettings,
    Project,
    ProjectMode,
    ProjectStatus,
    SourceSettings,
    now_iso,
)
from consultant_cli.domain.states import assert_stage_can_be_approved, transition
from consultant_cli.errors import InvalidConfigurationError, NotFoundError, WorkflowBlockedError
from consultant_cli.infrastructure import frontmatter
from consultant_cli.infrastructure.settings import AppSettings
from consultant_cli.infrastructure.store import (
    ProjectStore,
    RepositoryPaths,
    atomic_write_json,
    slugify,
)
from consultant_cli.services.agents import AgentService
from consultant_cli.services.examples import ExampleRegistry
from consultant_cli.services.generation import (
    ArtifactRenderer,
    GenerationContract,
    PromptBuilder,
    evidence_ndjson,
    instruction_validation_markdown,
)
from consultant_cli.services.modeler import ModelerReviewService
from consultant_cli.services.sources import SourceRouter


class WorkflowService:
    def __init__(
        self,
        paths: RepositoryPaths,
        store: ProjectStore,
        settings: AppSettings,
        agents: AgentService,
    ):
        self.paths = paths
        self.store = store
        self.settings = settings
        self.agents = agents
        self.sources = SourceRouter(paths)
        self.contract = GenerationContract(paths)
        self.prompts = PromptBuilder(paths)
        self.renderer = ArtifactRenderer()
        self.modeler = ModelerReviewService(paths)
        self.examples = ExampleRegistry(paths, store)

    def create_project(
        self,
        title: str,
        prompt: str,
        mode: str,
        product: str,
        edition: str = "",
        release: str = "",
        agent_profile: str = "",
        detail_level: str = "balanced",
        project_id: str | None = None,
    ) -> Project:
        if not title.strip():
            raise InvalidConfigurationError("Название проекта обязательно для заполнения.")
        if mode != ProjectMode.FULL.value:
            raise InvalidConfigurationError(
                "Поддерживается только полный режим с обязательными вопросами."
            )
        selected_mode = ProjectMode.FULL
        base_id = slugify(project_id or title)
        candidate = base_id
        suffix = 2
        while (self.paths.results / candidate).exists():
            candidate = f"{base_id}-{suffix}"
            suffix += 1
        generation = GenerationSettings(
            questions="required",
            follow_up_questions=True,
            diagram=True,
            detail_level=detail_level,
        )
        project = Project(
            project_id=candidate,
            title=title.strip(),
            mode=selected_mode,
            configuration=ConfigurationInfo(product.strip() or "not_configured", edition, release),
            generation=generation,
            sources=SourceSettings(),
            agent_profile=agent_profile or self.settings.default_agent,
        )
        self.store.create(project, prompt)
        return project

    def configure(self, project_id: str, values: dict[str, Any]) -> Project:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if "agent_profile" in values:
                project.agent_profile = str(values["agent_profile"])
            if "product" in values:
                project.configuration.product = str(values["product"])
            if "edition" in values:
                project.configuration.edition = str(values["edition"])
            if "release" in values:
                project.configuration.release = str(values["release"])
            if "questions" in values:
                if values["questions"] != "required":
                    raise WorkflowBlockedError(
                        "В полном режиме обязательные вопросы нельзя отключить."
                    )
                project.generation.questions = "required"
            if "follow_up_questions" in values:
                if not bool(values["follow_up_questions"]):
                    raise WorkflowBlockedError(
                        "В полном режиме дополнительные уточнения нельзя отключить."
                    )
                project.generation.follow_up_questions = True
            if "diagram" in values:
                if not bool(values["diagram"]):
                    raise WorkflowBlockedError(
                        "В полном режиме схему нельзя отключить."
                    )
                project.generation.diagram = True
            if "detail_level" in values:
                project.generation.detail_level = str(values["detail_level"])
            if "internet_policy" in values:
                project.sources.internet_policy = str(values["internet_policy"])
            self.store.save(project)
            self.store.append_event(project_id, "project_configured", values)
            return project

    def delete_project(self, project_id: str) -> Path:
        project = self.store.load(project_id)
        if project.status is ProjectStatus.GENERATING:
            raise WorkflowBlockedError(
                "Нельзя удалить проект во время генерации. Дождитесь завершения процесса."
            )
        destination = self.store.trash(project_id)
        self.examples.rebuild()
        return destination

    def run(self, project_id: str) -> tuple[Project, Path]:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            stage = self._next_stage(project)
            profile = self.agents.get_profile(project.agent_profile or None)
            request = self.store.read_artifact(project_id, "00-request.md")
            feedback_path = self.store.project_dir(project_id) / "feedback.md"
            if feedback_path.exists():
                request += "\n\nПоследние замечания:\n" + feedback_path.read_text(encoding="utf-8")
            route = self.sources.route(project.configuration, request)
            route_path = self.store.project_dir(project_id) / "source-route.json"
            atomic_write_json(route_path, route.to_dict())
            self.store.append_event(
                project_id,
                "sources_routed",
                {
                    "compatibility": route.compatibility,
                    "use_xml": route.use_xml,
                    "web_search_required": route.web_search_required,
                    "warnings": route.warnings,
                },
            )
            if route.web_search_required and project.sources.internet_policy == "forbidden":
                raise WorkflowBlockedError(
                    "Для выбранной конфигурации нет совместимого локального XML. "
                    "Разрешите официальные интернет-источники или добавьте подходящую "
                    "документацию в базу знаний."
                )
            if route.web_search_required and profile.kind in {
                "openai_compatible",
                "custom_cli",
            }:
                raise WorkflowBlockedError(
                    "Выбранный AI-профиль не гарантирует веб-поиск. Добавьте подходящие "
                    "документы/URL, используйте OpenAI Responses API или откройте проект "
                    "во внешнем агенте с веб-доступом."
                )
            existing = self._existing_context(project, stage)
            example_context = self._example_context(project)
            if example_context:
                existing += "\n\nСовместимые подтверждённые примеры:\n" + example_context
            prompt = self.prompts.build(
                project,
                stage,
                request,
                self.sources.context(route),
                existing,
                self.modeler.context(project, request),
            )
            previous_status = project.status
            transition(project, ProjectStatus.GENERATING)
            self.store.save(project)
            try:
                result = self.agents.generate(
                    profile,
                    prompt,
                    self.contract.schema(),
                    allow_web_search=(
                        route.web_search_required
                        and project.sources.internet_policy != "forbidden"
                    ),
                )
                self.contract.validate(result, stage, project, route)
                artifact_path = self._save_generation(project, stage, result)
                self.store.write_artifact(project_id, "evidence.ndjson", evidence_ndjson(result))
                self.store.append_event(
                    project_id,
                    f"{stage}_generated",
                    {"agent_profile": profile.name, "artifact": artifact_path.name},
                )
                project.last_error = ""
                self.store.save(project)
                return project, artifact_path
            except Exception as exc:
                # A failed model response must not revoke an already approved gate.
                # Restore the stage that was valid before generation so the same
                # operation can be retried after correcting the prompt or source.
                project.status = previous_status
                project.last_error = str(exc)
                self.store.save(project)
                self.store.append_event(project_id, "generation_failed", {"error": str(exc)})
                raise

    def _next_stage(self, project: Project) -> str:
        if project.status in {
            ProjectStatus.REQUIREMENTS_PENDING,
            ProjectStatus.DESIGN_PENDING,
            ProjectStatus.FEEDBACK_PENDING,
            ProjectStatus.SUCCESSFUL,
        }:
            raise WorkflowBlockedError(
                f"Текущий этап ожидает действия консультанта: {project.status.value}."
            )
        if project.requirements_version == 0:
            return "questions"
        if project.status is ProjectStatus.REQUIREMENTS_APPROVED:
            return "design"
        if project.design_version == 0:
            raise WorkflowBlockedError("Сначала утвердите требования.")
        if project.status not in {
            ProjectStatus.DESIGN_APPROVED,
            ProjectStatus.NEEDS_REVISION,
            ProjectStatus.DRAFT,
            ProjectStatus.ERROR,
        }:
            raise WorkflowBlockedError("Сначала утвердите проект и схему.")
        return "instruction"

    def _save_generation(
        self, project: Project, stage: str, result: dict[str, Any]
    ) -> Path:
        if stage == "questions":
            text = self.renderer.requirements(project, result)
            path = self.store.write_artifact(project.project_id, "01-requirements.md", text)
            atomic_write_json(
                self.store.project_dir(project.project_id) / "questions.json",
                result.get("questions", []),
            )
            project.requirements_version += 1
            transition(project, ProjectStatus.REQUIREMENTS_PENDING)
            return path
        if stage == "design":
            text = self.renderer.design(project, result)
            path = self.store.write_artifact(project.project_id, "02-design.md", text)
            project.design_version += 1
            transition(project, ProjectStatus.DESIGN_PENDING)
            return path
        text = self.renderer.instruction(project, result)
        path = self.store.write_artifact(project.project_id, "03-instruction.md", text)
        self.store.write_artifact(
            project.project_id,
            "03-instruction.json",
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        )
        self.store.write_artifact(
            project.project_id,
            "03-instruction-validation.md",
            instruction_validation_markdown(result),
        )
        modeler_review = self.modeler.review(project, result)
        self.store.write_artifact(
            project.project_id,
            "03-modeler-review.json",
            json.dumps(modeler_review, ensure_ascii=False, indent=2) + "\n",
        )
        self.store.write_artifact(
            project.project_id,
            "03-modeler-review.md",
            self.modeler.markdown(modeler_review),
        )
        project.instruction_version += 1
        transition(project, ProjectStatus.FEEDBACK_PENDING)
        return path

    def questions(self, project_id: str) -> list[dict[str, Any]]:
        path = self.store.project_dir(project_id) / "questions.json"
        if not path.exists():
            raise NotFoundError("Пакет вопросов ещё не сформирован.")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_answers(self, project_id: str, answers: dict[str, str]) -> Path:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if project.status is not ProjectStatus.REQUIREMENTS_PENDING:
                raise WorkflowBlockedError("Ответы принимаются только на этапе требований.")
            questions = self.questions(project_id)
            known = {str(item.get("id")) for item in questions}
            unexpected = sorted(set(answers) - known)
            if unexpected:
                raise WorkflowBlockedError(f"Неизвестные идентификаторы вопросов: {unexpected}")
            path = self.store.project_dir(project_id) / "answers.json"
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            existing.update({key: value.strip() for key, value in answers.items()})
            atomic_write_json(path, existing)
            body = ["# Ответы консультанта", ""]
            for question in questions:
                question_id = str(question.get("id"))
                body.extend(
                    [
                        f"## {question_id}. {question.get('text', '')}",
                        "",
                        existing.get(question_id, "_Нет ответа._"),
                        "",
                    ]
                )
            answer_md = self.store.write_artifact(project_id, "01-answers.md", "\n".join(body))
            self.store.append_event(
                project_id, "answers_saved", {"answered": sorted(existing)}
            )
            return answer_md

    def approve(
        self, project_id: str, stage: str, approved_by: str, evidence: str
    ) -> Project:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            assert_stage_can_be_approved(project, stage)
            if not approved_by.strip() or not evidence.strip():
                raise WorkflowBlockedError("Нужны согласовавший и явное основание апрува.")
            artifact_name = {
                "requirements": "01-requirements.md",
                "design": "02-design.md",
                "instruction": "03-instruction.md",
            }[stage]
            if stage == "requirements":
                self._assert_answers_complete(project_id)
                target = ProjectStatus.REQUIREMENTS_APPROVED
            elif stage == "design":
                target = ProjectStatus.DESIGN_APPROVED
            else:
                self._assert_instruction_verifiable(project_id)
                target = ProjectStatus.SUCCESSFUL
            text = self.store.read_artifact(project_id, artifact_name)
            values = {
                "approval_status": "approved",
                "approved_by": approved_by.strip(),
                "approved_at": now_iso(),
                "approval_evidence": evidence.strip(),
            }
            if stage == "instruction":
                values["review_status"] = "successful"
            self.store.write_artifact(
                project_id, artifact_name, frontmatter.update(text, **values)
            )
            transition(project, target)
            project.last_error = ""
            self.store.save(project)
            self.store.append_event(
                project_id,
                f"{stage}_approved",
                {"approved_by": approved_by.strip(), "evidence": evidence.strip()},
            )
            if stage == "instruction":
                self.examples.rebuild()
            return project

    def _assert_instruction_verifiable(self, project_id: str) -> None:
        directory = self.store.project_dir(project_id)
        stage_blockers: list[str] = []
        for name, label in (
            ("01-requirements.md", "требования"),
            ("02-design.md", "проект и схема"),
        ):
            path = directory / name
            if not path.exists():
                stage_blockers.append(f"отсутствует этап «{label}»")
                continue
            metadata, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
            if metadata.get("approval_status") != "approved":
                stage_blockers.append(f"не утверждён этап «{label}»")
        if stage_blockers:
            raise WorkflowBlockedError(
                "Инструкцию нельзя перевести в successful: "
                + "; ".join(stage_blockers)
                + ". Запустите полный цикл с обязательными вопросами и апрувами."
            )
        validation_path = directory / "03-instruction-validation.md"
        modeler_path = directory / "03-modeler-review.json"
        if not validation_path.exists() or not modeler_path.exists():
            raise WorkflowBlockedError(
                "Нет полного отчёта проверки инструкции. Перегенерируйте текущую версию."
            )
        validation = validation_path.read_text(encoding="utf-8")
        try:
            modeler = json.loads(modeler_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowBlockedError("Отчёт Modeler повреждён.") from exc
        blockers: list[str] = []
        if "`contract_passed`" not in validation:
            blockers.append("есть шаги inferred или без точного UI-пути")
        if modeler.get("verdict") != "passed":
            summary = modeler.get("summary", {})
            blockers.append(
                "Modeler не подтвердил все пути "
                f"(inferred={summary.get('inferred', 0)}, "
                f"unresolved={summary.get('unresolved', 0)})"
            )
        if blockers:
            raise WorkflowBlockedError(
                "Инструкцию нельзя перевести в successful: " + "; ".join(blockers) + ". "
                "Исправьте или удалите неподтверждённые шаги и сформируйте новую версию."
            )

    def _assert_answers_complete(self, project_id: str) -> None:
        questions = self.questions(project_id)
        path = self.store.project_dir(project_id) / "answers.json"
        answers = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        missing = [
            str(question.get("id"))
            for question in questions
            if question.get("required", True)
            and not str(answers.get(str(question.get("id")), "")).strip()
        ]
        if missing:
            raise WorkflowBlockedError(
                "Не заполнены обязательные вопросы: " + ", ".join(missing)
            )

    def request_changes(self, project_id: str, reason: str, by: str = "consultant") -> Project:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if project.status not in {
                ProjectStatus.FEEDBACK_PENDING,
                ProjectStatus.SUCCESSFUL,
                ProjectStatus.DRAFT,
            }:
                raise WorkflowBlockedError(
                    f"Замечания нельзя принять в статусе {project.status.value}."
                )
            was_successful = project.status is ProjectStatus.SUCCESSFUL
            instruction = self.store.read_artifact(project_id, "03-instruction.md")
            values: dict[str, Any] = {"review_status": "needs_revision"}
            if was_successful:
                values.update(
                    approval_status="revoked",
                    approval_revoked_at=now_iso(),
                    approval_revoked_by=by,
                    approval_revocation_reason=reason.strip(),
                )
            self.store.write_artifact(
                project_id, "03-instruction.md", frontmatter.update(instruction, **values)
            )
            self.store.write_artifact(
                project_id,
                "feedback.md",
                f"# Замечания к инструкции\n\nАвтор: {by}\n\n{reason.strip()}\n",
            )
            transition(project, ProjectStatus.NEEDS_REVISION)
            self.store.save(project)
            self.store.append_event(
                project_id,
                "instruction_approval_revoked" if was_successful else "changes_requested",
                {"by": by, "reason": reason.strip()},
            )
            self.examples.rebuild()
            return project

    def revise_design(
        self, project_id: str, reason: str, by: str = "consultant"
    ) -> Project:
        """Return a pending design to generation without revoking requirements."""
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if project.status is not ProjectStatus.DESIGN_PENDING:
                raise WorkflowBlockedError(
                    "Доработка схемы доступна только до её утверждения."
                )
            if not reason.strip():
                raise WorkflowBlockedError("Укажите, что нужно изменить в проекте и схеме.")
            self.store.write_artifact(
                project_id,
                "feedback.md",
                "# Замечания к проекту и схеме\n\n"
                f"Автор: {by}\n\n{reason.strip()}\n",
            )
            transition(project, ProjectStatus.REQUIREMENTS_APPROVED)
            self.store.save(project)
            self.store.append_event(
                project_id,
                "design_changes_requested",
                {"by": by, "reason": reason.strip(), "previous_version": project.design_version},
            )
            return project

    def save_draft(self, project_id: str) -> Project:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if project.status is not ProjectStatus.FEEDBACK_PENDING:
                raise WorkflowBlockedError("Сейчас нет результата, ожидающего обратную связь.")
            transition(project, ProjectStatus.DRAFT)
            self.store.save(project)
            self.store.append_event(project_id, "instruction_saved_as_draft")
            return project

    def _existing_context(self, project: Project, stage: str) -> str:
        directory = self.store.project_dir(project.project_id)
        names = []
        if stage in {"design", "instruction"}:
            names.extend(["01-requirements.md", "01-answers.md"])
        if stage == "instruction":
            names.append("02-design.md")
        if stage == "instruction" and project.instruction_version:
            names.extend(["03-instruction.md", "feedback.md"])
        blocks = []
        for name in names:
            path = directory / name
            if path.exists():
                blocks.append(f"FILE {name}\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join(blocks)

    def _example_context(self, project: Project) -> str:
        records = self.examples.compatible(
            project.configuration.product, project.configuration.release
        )
        blocks = []
        for record in records[:3]:
            path = self.paths.root / record["instruction_path"]
            if path.exists() and record["project_id"] != project.project_id:
                blocks.append(
                    f"EXAMPLE {record['project_id']} ({record['instruction_path']})\n"
                    + path.read_text(encoding="utf-8")[:6000]
                )
        return "\n\n".join(blocks)
