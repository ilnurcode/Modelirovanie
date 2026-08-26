from __future__ import annotations

import json
import copy
import hashlib
import os
import re
import time
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
from consultant_cli.errors import (
    GenerationValidationError,
    InvalidConfigurationError,
    NotFoundError,
    WorkflowBlockedError,
)
from consultant_cli.infrastructure import frontmatter
from consultant_cli.infrastructure.settings import AppSettings
from consultant_cli.infrastructure.store import (
    ProjectStore,
    RepositoryPaths,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
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
from consultant_cli.services.analytics import AnalyticsService
from consultant_cli.services.telemetry import TelemetryService
from consultant_cli.services.graph_search import GraphSearchService


ROLE_BY_STAGE = {
    "questions": "erp-translator",
    "design": "erp-process-planner",
    "instruction": "instruction-writer",
}


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
        self.analytics = AnalyticsService(paths, store)
        self.telemetry = TelemetryService(paths, store)
        self.graph = GraphSearchService(paths)

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
        deliverable: str = "hybrid",
        project_id: str | None = None,
        source_name: str = "",
        source_bytes: bytes | None = None,
    ) -> Project:
        if not title.strip():
            raise InvalidConfigurationError("Название проекта обязательно для заполнения.")
        if mode != ProjectMode.FULL.value:
            raise InvalidConfigurationError(
                "Поддерживается только полный режим с обязательными вопросами."
            )
        selected_mode = ProjectMode.FULL
        if deliverable not in {"hybrid", "process", "consultant", "vanessa"}:
            raise InvalidConfigurationError(f"Неизвестный формат результата: {deliverable}")
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
            deliverable=deliverable,
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
        if source_name:
            exact_source = source_bytes if source_bytes is not None else prompt.encode("utf-8")
            atomic_write_bytes(
                self.store.project_dir(project.project_id) / "00-source.md",
                exact_source,
            )
            self.store.append_event(
                project.project_id,
                "project_created_from_markdown",
                {
                    "source_name": Path(source_name).name,
                    "sha256": hashlib.sha256(exact_source).hexdigest(),
                },
            )
        self.analytics.initialize(project.project_id, prompt, project.configuration)
        return project

    def configure(self, project_id: str, values: dict[str, Any]) -> Project:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            previous_configuration = (
                project.configuration.product,
                project.configuration.edition,
                project.configuration.release,
            )
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
            if "deliverable" in values:
                deliverable = str(values["deliverable"])
                if deliverable not in {"hybrid", "process", "consultant", "vanessa"}:
                    raise WorkflowBlockedError(f"Неизвестный формат результата: {deliverable}")
                project.generation.deliverable = deliverable
            if "internet_policy" in values:
                project.sources.internet_policy = str(values["internet_policy"])
            self.store.save(project)
            if previous_configuration != (
                project.configuration.product,
                project.configuration.edition,
                project.configuration.release,
            ):
                self.analytics.reconfigure(project_id, project.configuration)
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

    def record_decision(self, project_id: str, question_id: str, answer: str) -> dict[str, Any]:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if project.status is ProjectStatus.GENERATING:
                raise WorkflowBlockedError("Нельзя менять решение во время генерации.")
            result = self.analytics.record_decision(project_id, question_id, answer)
            if not result.get("recorded"):
                return result
            project.revision = int(result["revision"])
            if project.status in {
                ProjectStatus.REQUIREMENTS_APPROVED,
                ProjectStatus.DESIGN_PENDING,
                ProjectStatus.DESIGN_APPROVED,
            }:
                transition(project, ProjectStatus.REQUIREMENTS_PENDING)
            elif project.status in {
                ProjectStatus.FEEDBACK_PENDING,
                ProjectStatus.DRAFT,
                ProjectStatus.SUCCESSFUL,
                ProjectStatus.ERROR,
            }:
                transition(project, ProjectStatus.NEEDS_REVISION)
            self.store.save(project)
            self.store.append_event(project_id, "decision_recorded", result)
            self.examples.rebuild()
            return result

    def run(self, project_id: str) -> tuple[Project, Path]:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            stage = self._next_stage(project)
            analysis = self.analytics.ensure(project_id)
            if analysis.dirty_clusters:
                self.analytics.analyze_evidence(project_id, analysis.dirty_clusters)
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
            role = ROLE_BY_STAGE[stage]
            use_role_runtime = hasattr(self.agents, "generate_role") and hasattr(
                self.agents, "role_profile"
            )
            profile = (
                self.agents.role_profile(role)
                if use_role_runtime
                else self.agents.get_profile(project.agent_profile or None)
            )
            if route.web_search_required and project.sources.internet_policy == "forbidden":
                raise WorkflowBlockedError(
                    "Для выбранной конфигурации нет совместимого локального XML. "
                    "Разрешите официальные интернет-источники или добавьте подходящую "
                    "документацию в базу знаний."
                )
            existing = self._existing_context(project, stage)
            existing += "\n\nUNIFIED ANALYTICS (compact JSON)\n" + self.analytics.author_context(project_id)
            example_context = self._example_context(project)
            if example_context:
                existing += "\n\nСовместимые подтверждённые примеры:\n" + example_context
            graph_context = self.graph.project_context(
                project_id, request, project.revision
            )
            modeler_context = (
                ""
                if stage == "questions"
                else self.modeler.context(project, request)
            )
            prompt = self.prompts.build(
                project,
                stage,
                request,
                self.sources.context(route),
                existing,
                modeler_context,
                graph_context,
                role,
            )
            previous_status = project.status
            transition(project, ProjectStatus.GENERATING)
            self.store.save(project)
            started = time.perf_counter()
            attempt = 1 + sum(
                1
                for item in self.telemetry.records(project_id)
                if item.get("skill") == role
            )
            trace_stem = f"{stage}-r{project.revision:03d}-a{attempt:03d}"
            prompt_path, execution_path = self._trace_paths(project_id, trace_stem)
            atomic_write_text(prompt_path, prompt)
            self.store.append_event(
                project_id,
                "api_role_started",
                {
                    "stage": stage,
                    "role": role,
                    "model": profile.model,
                    "attempt": attempt,
                },
            )
            try:
                generation_args = (
                    role,
                    prompt,
                    self.contract.schema(),
                ) if use_role_runtime else (
                    profile,
                    prompt,
                    self.contract.schema(),
                )
                generator = self.agents.generate_role if use_role_runtime else self.agents.generate
                result = generator(
                    *generation_args,
                    allow_web_search=(
                        route.web_search_required
                        and project.sources.internet_policy != "forbidden"
                    ),
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                execution = self._write_execution_trace(
                    execution_path,
                    prompt_path=prompt_path,
                    role=role,
                    profile=profile,
                    attempt=attempt,
                    status="api_response_received",
                    duration_ms=elapsed_ms,
                    usage=getattr(self.agents, "last_usage", {}),
                )
                raw_result_path = (
                    self.store.project_dir(project_id)
                    / "agent_artifacts"
                    / f"{stage}-raw-r{project.revision:03d}-a{attempt:03d}.json"
                )
                atomic_write_json(raw_result_path, result)
                question_repairs = (
                    self._supplement_question_coverage(project_id, result)
                    if stage == "questions"
                    else []
                )
                if question_repairs:
                    self.store.append_event(
                        project_id,
                        "question_coverage_supplemented",
                        {"questions": question_repairs, "llm_calls": 0},
                    )
                ref_repairs = self.contract.normalize_known_project_refs(result, project)
                if ref_repairs:
                    self.store.append_event(
                        project_id,
                        "known_source_refs_normalized",
                        {"stage": stage, "repairs": ref_repairs},
                    )
                missing_ref_repairs = self.contract.normalize_missing_local_refs(result)
                if missing_ref_repairs:
                    self.store.append_event(
                        project_id,
                        "missing_local_refs_normalized",
                        {"stage": stage, "repairs": missing_ref_repairs},
                    )
                unavailable_source_repairs = (
                    self.contract.normalize_unavailable_inferred_sources(result)
                )
                if unavailable_source_repairs:
                    self.store.append_event(
                        project_id,
                        "unavailable_inferred_sources_removed",
                        {"stage": stage, "repairs": unavailable_source_repairs},
                    )
                official_url_repairs = self.contract.normalize_required_official_url(
                    result, route
                )
                if official_url_repairs:
                    self.store.append_event(
                        project_id,
                        "official_url_restored_from_routed_knowledge",
                        {"stage": stage, "repairs": official_url_repairs, "network_calls": 0},
                    )
                flow_repairs = (
                    self.graph.normalize_document_flow(result)
                    if stage != "questions"
                    else []
                )
                if flow_repairs:
                    self.store.append_event(
                        project_id,
                        "document_flow_normalized",
                        {"stage": stage, "repairs": flow_repairs},
                    )
                evidence_repairs = self.contract.normalize_evidence_refs(result)
                if evidence_repairs:
                    self.store.append_event(
                        project_id,
                        "evidence_refs_normalized",
                        {"stage": stage, "repairs": evidence_repairs},
                    )
                modeler_path_repairs = (
                    self.modeler.normalize_ui_paths(project, result)
                    if stage == "instruction"
                    else []
                )
                if modeler_path_repairs:
                    self.store.append_event(
                        project_id,
                        "ui_paths_expanded_from_modeler",
                        {"stage": stage, "repairs": modeler_path_repairs},
                    )
                vanessa_path_repairs = self.contract.normalize_vanessa_ui_paths(
                    result, modeler_path_repairs
                )
                if vanessa_path_repairs:
                    self.store.append_event(
                        project_id,
                        "vanessa_ui_paths_expanded_from_modeler",
                        {"stage": stage, "repairs": vanessa_path_repairs},
                    )
                verification_status_repairs = (
                    self.contract.normalize_known_verification_statuses(result)
                )
                if verification_status_repairs:
                    self.store.append_event(
                        project_id,
                        "known_verification_statuses_normalized",
                        {"stage": stage, "repairs": verification_status_repairs},
                    )
                self.contract.validate(result, stage, project, route)
                flow_errors = self.graph.document_flow_errors(result) if stage != "questions" else []
                if flow_errors:
                    raise GenerationValidationError("; ".join(flow_errors))
                atomic_write_json(
                    self.store.project_dir(project_id)
                    / "agent_artifacts"
                    / f"{stage}-r{project.revision:03d}.json",
                    result,
                )
                artifact_path = self._save_generation(project, stage, result)
                self.store.write_artifact(project_id, "evidence.ndjson", evidence_ndjson(result))
                self.store.append_event(
                    project_id,
                    f"{stage}_generated",
                    {"role": role, "model": profile.model, "artifact": artifact_path.name},
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                execution = self._write_execution_trace(
                    execution_path,
                    prompt_path=prompt_path,
                    role=role,
                    profile=profile,
                    attempt=attempt,
                    status="completed",
                    duration_ms=elapsed_ms,
                    usage=getattr(self.agents, "last_usage", {}),
                )
                self._attach_execution_trace(artifact_path, prompt_path, execution)
                if stage == "instruction":
                    atomic_write_text(
                        self.store.project_dir(project_id)
                        / "answers_md"
                        / f"instruction-v{project.instruction_version:03d}-draft.md",
                        artifact_path.read_text(encoding="utf-8"),
                    )
                self.telemetry.record(
                    project_id,
                    provider=profile.kind,
                    model=profile.model or profile.command or profile.name,
                    reasoning_effort=profile.reasoning_effort,
                    skill=role,
                    attempt=attempt,
                    result="completed",
                    duration_ms=elapsed_ms,
                    wall_time_ms=elapsed_ms,
                    **getattr(self.agents, "last_usage", {}),
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
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                self._write_execution_trace(
                    execution_path,
                    prompt_path=prompt_path,
                    role=role,
                    profile=profile,
                    attempt=attempt,
                    status="failed",
                    duration_ms=elapsed_ms,
                    usage=getattr(self.agents, "last_usage", {}),
                    error=str(exc),
                )
                self.telemetry.record(
                    project_id,
                    provider=profile.kind,
                    model=profile.model or profile.command or profile.name,
                    reasoning_effort=profile.reasoning_effort,
                    skill=role,
                    attempt=attempt,
                    result="failed",
                    error=str(exc),
                    duration_ms=elapsed_ms,
                    wall_time_ms=elapsed_ms,
                    **getattr(self.agents, "last_usage", {}),
                )
                raise

    def recover_latest_generation(self, project_id: str) -> tuple[Project, Path]:
        """Revalidate and save the latest raw response without another API call."""
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            preserve_pending_design = project.status is ProjectStatus.DESIGN_PENDING
            preserve_pending_instruction = project.status is ProjectStatus.FEEDBACK_PENDING
            stage = (
                "design"
                if preserve_pending_design
                else "instruction"
                if preserve_pending_instruction
                else self._next_stage(project)
            )
            candidates = sorted(
                (
                    self.store.project_dir(project_id)
                    / "agent_artifacts"
                ).glob(
                    f"{stage}-raw-r*-a*.json"
                    if preserve_pending_design or preserve_pending_instruction
                    else f"{stage}-raw-r{project.revision:03d}-a*.json"
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            if not candidates:
                raise NotFoundError(
                    f"Сохранённый сырой ответ для этапа {stage} не найден."
                )
            raw_path = candidates[0]
            result = json.loads(raw_path.read_text(encoding="utf-8"))
            request = self.store.read_artifact(project_id, "00-request.md")
            route = self.sources.route(project.configuration, request)
            question_repairs = (
                self._supplement_question_coverage(project_id, result)
                if stage == "questions"
                else []
            )
            ref_repairs = self.contract.normalize_known_project_refs(result, project)
            missing_ref_repairs = self.contract.normalize_missing_local_refs(result)
            unavailable_source_repairs = (
                self.contract.normalize_unavailable_inferred_sources(result)
            )
            official_url_repairs = self.contract.normalize_required_official_url(
                result, route
            )
            flow_repairs = (
                self.graph.normalize_document_flow(result)
                if stage != "questions"
                else []
            )
            evidence_repairs = self.contract.normalize_evidence_refs(result)
            modeler_path_repairs = (
                self.modeler.normalize_ui_paths(project, result)
                if stage == "instruction"
                else []
            )
            vanessa_path_repairs = self.contract.normalize_vanessa_ui_paths(
                result, modeler_path_repairs
            )
            verification_status_repairs = (
                self.contract.normalize_known_verification_statuses(result)
            )
            self.contract.validate(result, stage, project, route)
            flow_errors = self.graph.document_flow_errors(result) if stage != "questions" else []
            if flow_errors:
                raise GenerationValidationError("; ".join(flow_errors))
            atomic_write_json(
                self.store.project_dir(project_id)
                / "agent_artifacts"
                / f"{stage}-r{project.revision:03d}.json",
                result,
            )
            if preserve_pending_design:
                artifact_path = self.store.write_artifact(
                    project.project_id,
                    "02-design.md",
                    self.renderer.design(project, result),
                )
            elif preserve_pending_instruction:
                render_project = copy.deepcopy(project)
                render_project.instruction_version = max(0, project.instruction_version - 1)
                text = self.renderer.instruction(render_project, result)
                artifact_path = self.store.write_artifact(
                    project.project_id, "03-instruction.md", text
                )
                answer_dir = self.store.project_dir(project.project_id) / "answers_md"
                version = project.instruction_version
                atomic_write_text(
                    answer_dir / f"instruction-v{version:03d}-draft.md", text
                )
                atomic_write_json(
                    answer_dir / f"instruction-v{version:03d}-draft.json", result
                )
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
            else:
                artifact_path = self._save_generation(project, stage, result)
            trace_stem = raw_path.stem.replace("-raw-", "-")
            prompt_path, execution_path = self._trace_paths(project_id, trace_stem)
            saved_execution = self._read_execution_trace(execution_path)
            execution = saved_execution or {
                "role": ROLE_BY_STAGE[stage],
                "model": "не записано в старом вызове",
                "attempt": None,
                "status": "recovered_without_api",
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": None,
                "duration_ms": None,
                "error": "",
            }
            if saved_execution:
                execution["original_status"] = saved_execution.get("status", "")
                execution["status"] = "recovered_without_api"
                execution["error"] = ""
                atomic_write_json(execution_path, execution)
            self._attach_execution_trace(
                artifact_path,
                prompt_path if prompt_path.is_file() else None,
                execution,
            )
            if stage == "instruction":
                atomic_write_text(
                    self.store.project_dir(project_id)
                    / "answers_md"
                    / f"instruction-v{project.instruction_version:03d}-draft.md",
                    artifact_path.read_text(encoding="utf-8"),
                )
            self.store.write_artifact(project_id, "evidence.ndjson", evidence_ndjson(result))
            project.last_error = ""
            self.store.save(project)
            self.store.append_event(
                project_id,
                f"{stage}_recovered_from_raw",
                {
                    "raw_artifact": raw_path.name,
                    "artifact": artifact_path.name,
                    "known_ref_repairs": ref_repairs,
                    "missing_local_ref_repairs": missing_ref_repairs,
                    "unavailable_source_repairs": unavailable_source_repairs,
                    "official_url_repairs": official_url_repairs,
                    "document_flow_repairs": flow_repairs,
                    "evidence_ref_repairs": evidence_repairs,
                    "modeler_path_repairs": modeler_path_repairs,
                    "vanessa_path_repairs": vanessa_path_repairs,
                    "verification_status_repairs": verification_status_repairs,
                    "question_coverage_supplements": question_repairs,
                    "api_calls": 0,
                },
            )
            return project, artifact_path

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
            synchronized = self.analytics.synchronize_presented_questions(
                project.project_id, result.get("questions", [])
            )
            text = self.renderer.requirements(project, result)
            text = text.rstrip() + self._question_coverage_section(synchronized) + "\n"
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
        answer_dir = self.store.project_dir(project.project_id) / "answers_md"
        answer_version = project.instruction_version + 1
        atomic_write_text(answer_dir / f"instruction-v{answer_version:03d}-draft.md", text)
        atomic_write_json(
            answer_dir / f"instruction-v{answer_version:03d}-draft.json",
            result,
        )
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

    def refresh_question_audit(self, project_id: str) -> Path:
        """Add the local coverage/usage audit to a legacy question artifact."""
        with self.store.lock(project_id):
            artifact_path = self.store.project_dir(project_id) / "01-requirements.md"
            if not artifact_path.is_file():
                raise NotFoundError("Артефакт требований ещё не сформирован.")
            bundle = self.analytics.load(project_id)
            text = artifact_path.read_text(encoding="utf-8")
            text = text.split("\n## Аудит полноты вопросов\n", 1)[0]
            text = text.split("\n## Выполнение AI\n", 1)[0]
            atomic_write_text(
                artifact_path,
                text.rstrip() + self._question_coverage_section(bundle) + "\n",
            )
            calls = [
                item
                for item in self.telemetry.records(project_id)
                if item.get("skill") == "erp-translator"
            ]
            latest = calls[-1] if calls else {}
            input_tokens = latest.get("input_tokens") if latest else None
            output_tokens = latest.get("output_tokens") if latest else None
            execution = {
                "role": "erp-translator",
                "model": latest.get("model", "не записано в старом вызове"),
                "status": latest.get("result", "legacy_call"),
                "input_tokens": input_tokens,
                "cached_input_tokens": latest.get("cached_input_tokens") if latest else None,
                "output_tokens": output_tokens,
                "reasoning_tokens": latest.get("reasoning_tokens") if latest else None,
                "total_tokens": (
                    int(input_tokens or 0) + int(output_tokens or 0)
                    if input_tokens is not None and output_tokens is not None
                    else None
                ),
                "duration_ms": latest.get("duration_ms") if latest else None,
            }
            prompt_candidates = sorted(
                (self.store.project_dir(project_id) / "agent_artifacts").glob(
                    "questions-r*-a*-prompt.txt"
                ),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            self._attach_execution_trace(
                artifact_path,
                prompt_candidates[0] if prompt_candidates else None,
                execution,
            )
            self.store.append_event(
                project_id,
                "question_audit_refreshed",
                {"api_calls": 0, "prompt_available": bool(prompt_candidates)},
            )
            return artifact_path

    def ask_project(
        self, project_id: str, question: str, kind: str = "process"
    ) -> dict[str, Any]:
        """Answer a saved project question without changing lifecycle approvals."""
        if kind not in {"process", "consultant", "vanessa", "implementation"}:
            raise WorkflowBlockedError(f"Неизвестный вид ответа: {kind}")
        if not question.strip():
            raise WorkflowBlockedError("Вопрос по проекту не должен быть пустым.")
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            self.analytics.ensure(project_id)
            query_id = hashlib.sha256(
                f"{project.revision}\0{kind}\0{question.strip()}".encode("utf-8")
            ).hexdigest()[:12]
            answers_dir = self.store.project_dir(project_id) / "answers_md"
            answer_path = answers_dir / f"query-{query_id}.md"
            if answer_path.is_file():
                return {
                    "project_id": project_id,
                    "query_id": query_id,
                    "path": str(answer_path),
                    "reused": True,
                }
            request = self.store.read_artifact(project_id, "00-request.md")
            route = self.sources.route(project.configuration, request + "\n" + question)
            graph_context = self.graph.project_context(
                project_id, question, project.revision
            )
            temporary = copy.deepcopy(project)
            temporary.generation.deliverable = {
                "process": "process",
                "implementation": "process",
                "consultant": "consultant",
                "vanessa": "vanessa",
            }[kind]
            role = "erp-process-planner" if kind in {"process", "implementation"} else "instruction-writer"
            existing = self._existing_context(project, "instruction")
            existing += "\n\nUNIFIED ANALYTICS\n" + self.analytics.author_context(project_id)
            prompt = self.prompts.build(
                temporary,
                "design",
                question,
                self.sources.context(route),
                existing,
                self.modeler.context(project, question),
                graph_context,
                role,
            )
            profile = self.agents.role_profile(role)
            started = time.perf_counter()
            prompt_path, execution_path = self._trace_paths(project_id, f"query-{query_id}")
            atomic_write_text(prompt_path, prompt)
            raw_query_path = answers_dir / f"query-{query_id}-raw.json"
            recovered_raw = raw_query_path.is_file()
            if recovered_raw:
                result = json.loads(raw_query_path.read_text(encoding="utf-8"))
                execution = self._read_execution_trace(execution_path) or {
                    "role": role,
                    "model": profile.model,
                    "attempt": None,
                    "status": "recovered_without_api",
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "total_tokens": None,
                    "duration_ms": None,
                    "error": "",
                }
            else:
                try:
                    result = self.agents.generate_role(role, prompt, self.contract.schema())
                except Exception as exc:
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    self._write_execution_trace(
                        execution_path,
                        prompt_path=prompt_path,
                        role=role,
                        profile=profile,
                        attempt=1,
                        status="failed",
                        duration_ms=elapsed_ms,
                        usage=getattr(self.agents, "last_usage", {}),
                        error=str(exc),
                    )
                    self.telemetry.record(
                        project_id,
                        provider=profile.kind,
                        model=profile.model,
                        reasoning_effort=profile.reasoning_effort,
                        skill=f"project-query:{role}",
                        attempt=1,
                        result="failed",
                        error=str(exc),
                        duration_ms=elapsed_ms,
                        wall_time_ms=elapsed_ms,
                        **getattr(self.agents, "last_usage", {}),
                    )
                    raise
                atomic_write_json(raw_query_path, result)
                execution = self._write_execution_trace(
                    execution_path,
                    prompt_path=prompt_path,
                    role=role,
                    profile=profile,
                    attempt=1,
                    status="api_response_received",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    usage=getattr(self.agents, "last_usage", {}),
                )
            ref_repairs = self.contract.normalize_known_project_refs(result, temporary)
            if ref_repairs:
                self.store.append_event(
                    project_id,
                    "known_source_refs_normalized",
                    {"stage": "project-query", "repairs": ref_repairs},
                )
            missing_ref_repairs = self.contract.normalize_missing_local_refs(result)
            if missing_ref_repairs:
                self.store.append_event(
                    project_id,
                    "missing_local_refs_normalized",
                    {"stage": "project-query", "repairs": missing_ref_repairs},
                )
            unavailable_source_repairs = (
                self.contract.normalize_unavailable_inferred_sources(result)
            )
            if unavailable_source_repairs:
                self.store.append_event(
                    project_id,
                    "unavailable_inferred_sources_removed",
                    {"stage": "project-query", "repairs": unavailable_source_repairs},
                )
            official_url_repairs = self.contract.normalize_required_official_url(
                result, route
            )
            if official_url_repairs:
                self.store.append_event(
                    project_id,
                    "official_url_restored_from_routed_knowledge",
                    {
                        "stage": "project-query",
                        "repairs": official_url_repairs,
                        "network_calls": 0,
                    },
                )
            flow_repairs = self.graph.normalize_document_flow(result)
            if flow_repairs:
                self.store.append_event(
                    project_id,
                    "document_flow_normalized",
                    {"stage": "project-query", "repairs": flow_repairs},
                )
            query_flow_repairs = self.graph.prune_non_document_flow(result)
            if query_flow_repairs:
                self.store.append_event(
                    project_id,
                    "project_query_non_documents_removed_from_flow",
                    {"stage": "project-query", "repairs": query_flow_repairs},
                )
            evidence_repairs = self.contract.normalize_evidence_refs(result)
            if evidence_repairs:
                self.store.append_event(
                    project_id,
                    "evidence_refs_normalized",
                    {"stage": "project-query", "repairs": evidence_repairs},
                )
            modeler_path_repairs = (
                self.modeler.normalize_ui_paths(temporary, result)
                if kind in {"consultant", "vanessa"}
                else []
            )
            if modeler_path_repairs:
                self.store.append_event(
                    project_id,
                    "ui_paths_expanded_from_modeler",
                    {"stage": "project-query", "repairs": modeler_path_repairs},
                )
            vanessa_path_repairs = self.contract.normalize_vanessa_ui_paths(
                result, modeler_path_repairs
            )
            if vanessa_path_repairs:
                self.store.append_event(
                    project_id,
                    "vanessa_ui_paths_expanded_from_modeler",
                    {"stage": "project-query", "repairs": vanessa_path_repairs},
                )
            metadata_status_repairs = self.contract.normalize_incompatible_metadata_steps(
                result,
                temporary,
                route,
                require_modeler_route=kind in {"consultant", "vanessa"},
            )
            if metadata_status_repairs:
                self.store.append_event(
                    project_id,
                    "project_query_metadata_status_normalized",
                    {"stage": "project-query", "repairs": metadata_status_repairs},
                )
            verification_status_repairs = (
                self.contract.normalize_known_verification_statuses(result)
            )
            if verification_status_repairs:
                self.store.append_event(
                    project_id,
                    "known_verification_statuses_normalized",
                    {"stage": "project-query", "repairs": verification_status_repairs},
                )
            self.contract.validate(
                result,
                "design",
                temporary,
                route,
                allow_empty_document_flow=True,
            )
            flow_errors = self.graph.document_flow_errors(result)
            if flow_errors:
                raise GenerationValidationError("; ".join(flow_errors))
            body = self.renderer._common_body(
                temporary, result, f"Ответ по проекту: {question.strip()}"
            )
            markdown = self.renderer._frontmatter(
                {
                    "artifact": "project-query-answer",
                    "project_id": project_id,
                    "project_revision": project.revision,
                    "query_id": query_id,
                    "kind": kind,
                    "role": role,
                    "model": profile.model,
                    "status": "unconfirmed",
                    "created_at": now_iso(),
                },
                body,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if not recovered_raw:
                execution = self._write_execution_trace(
                    execution_path,
                    prompt_path=prompt_path,
                    role=role,
                    profile=profile,
                    attempt=1,
                    status="completed",
                    duration_ms=elapsed_ms,
                    usage=getattr(self.agents, "last_usage", {}),
                )
            else:
                execution["original_status"] = execution.get("status", "")
                execution["status"] = "recovered_without_api"
                execution["error"] = ""
                atomic_write_json(execution_path, execution)
            atomic_write_text(answer_path, markdown)
            self._attach_execution_trace(answer_path, prompt_path, execution)
            atomic_write_json(answers_dir / f"query-{query_id}.json", result)
            index_path = answers_dir / "index.ndjson"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            with index_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(
                        {
                            "query_id": query_id,
                            "project_revision": project.revision,
                            "kind": kind,
                            "question": question.strip(),
                            "path": answer_path.name,
                            "created_at": now_iso(),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            if not recovered_raw:
                self.telemetry.record(
                    project_id,
                    provider=profile.kind,
                    model=profile.model,
                    reasoning_effort=profile.reasoning_effort,
                    skill=f"project-query:{role}",
                    attempt=1,
                    result="completed",
                    duration_ms=elapsed_ms,
                    wall_time_ms=elapsed_ms,
                    **getattr(self.agents, "last_usage", {}),
                )
            self.store.append_event(
                project_id,
                "project_query_answered",
                {
                    "query_id": query_id,
                    "kind": kind,
                    "path": answer_path.name,
                    "recovered_without_api": recovered_raw,
                },
            )
            return {
                "project_id": project_id,
                "query_id": query_id,
                "path": str(answer_path),
                "reused": recovered_raw,
                "model": profile.model,
            }

    def _trace_paths(self, project_id: str, stem: str) -> tuple[Path, Path]:
        directory = self.store.project_dir(project_id) / "agent_artifacts"
        return directory / f"{stem}-prompt.txt", directory / f"{stem}-execution.json"

    @staticmethod
    def _write_execution_trace(
        path: Path,
        *,
        prompt_path: Path,
        role: str,
        profile: Any,
        attempt: int,
        status: str,
        duration_ms: int,
        usage: dict[str, Any],
        error: str = "",
    ) -> dict[str, Any]:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        model = (
            getattr(profile, "model", "")
            or getattr(profile, "command", "")
            or getattr(profile, "name", "")
        )
        payload = {
            "role": role,
            "model": model,
            "provider": getattr(profile, "kind", ""),
            "reasoning_effort": getattr(profile, "reasoning_effort", ""),
            "attempt": attempt,
            "status": status,
            "prompt_path": str(prompt_path),
            "input_tokens": input_tokens,
            "cached_input_tokens": int(usage.get("cached_input_tokens", 0) or 0),
            "output_tokens": output_tokens,
            "reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
            "total_tokens": input_tokens + output_tokens,
            "duration_ms": int(duration_ms),
            "error": error,
        }
        atomic_write_json(path, payload)
        return payload

    @staticmethod
    def _read_execution_trace(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _attach_execution_trace(
        artifact_path: Path,
        prompt_path: Path | None,
        execution: dict[str, Any],
    ) -> None:
        marker = "\n## Выполнение AI\n"
        text = artifact_path.read_text(encoding="utf-8")
        text = text.split(marker, 1)[0].rstrip()
        prompt_link = "не был сохранён в старом вызове"
        if prompt_path is not None and prompt_path.is_file():
            relative = os.path.relpath(prompt_path, artifact_path.parent).replace("\\", "/")
            prompt_link = f"[полный точный prompt]({relative})"

        def shown(value: Any) -> str:
            return "не записано" if value is None else str(value)

        section = [
            "",
            "## Выполнение AI",
            "",
            f"- Prompt: {prompt_link}.",
            f"- Роль / модель: `{execution.get('role', '')}` / `{execution.get('model', '')}`.",
            f"- Входные токены: **{shown(execution.get('input_tokens'))}**; "
            f"из них cached: **{shown(execution.get('cached_input_tokens'))}**.",
            f"- Выходные токены: **{shown(execution.get('output_tokens'))}**; "
            f"reasoning: **{shown(execution.get('reasoning_tokens'))}**.",
            f"- Всего токенов (input + output): **{shown(execution.get('total_tokens'))}**.",
            f"- Длительность: **{shown(execution.get('duration_ms'))} мс**; "
            f"статус: `{execution.get('status', '')}`.",
        ]
        atomic_write_text(artifact_path, text + "\n" + "\n".join(section) + "\n")

    def _question_coverage_section(self, bundle: Any) -> str:
        clusters = sorted({item.cluster for item in bundle.requirements})
        questioned = sorted({item.cluster for item in bundle.questions})
        heuristic = self.analytics.build_questions(bundle.requirements)
        heuristic_clusters = sorted({item.cluster for item in heuristic})
        missing = sorted(set(heuristic_clusters) - set(questioned))
        status = (
            "требуется ручная проверка сигналов: " + ", ".join(missing)
            if missing
            else "локальные эвристические сигналы покрыты"
        )
        return (
            "\n\n## Аудит полноты вопросов\n\n"
            f"- Атомарных требований в ТЗ: **{len(bundle.requirements)}**.\n"
            f"- Смысловых кластеров в ТЗ передано translator: **{len(clusters)}** "
            f"({', '.join(clusters) or 'нет'}).\n"
            f"- Сформировано вопросов: **{len(bundle.questions)}** (это не фиксированный лимит; "
            "допустимо от 1 до 12 по числу материальных неопределённостей).\n"
            f"- Кластеры с вопросами: {', '.join(questioned) or 'не определены'}.\n"
            f"- Независимый локальный контроль: **{status}**.\n"
            "\nКоличество вопросов само по себе не доказывает полноту: перед утверждением "
            "нужно проверить перечисленные кластеры и влияние каждого вопроса на REQ-id."
        )

    def _supplement_question_coverage(
        self, project_id: str, result: dict[str, Any]
    ) -> list[str]:
        """Add free deterministic questions for uncertainty clusters omitted by AI."""
        bundle = self.analytics.ensure(project_id)
        questions = result.setdefault("questions", [])
        represented_requirement_ids = {
            requirement_id
            for question in questions
            for requirement_id in re.findall(
                r"REQ-[0-9a-f]+", str(question.get("impact") or ""), re.IGNORECASE
            )
        }
        additions = []
        for candidate in self.analytics.build_questions(bundle.requirements):
            if represented_requirement_ids.intersection(candidate.requirement_ids):
                continue
            if len(questions) >= 12:
                raise GenerationValidationError(
                    "Translator сформировал 12 вопросов, но не покрыл локальный сигнал "
                    f"кластера {candidate.cluster}. Нужен новый пакет с точными REQ-id в impact."
                )
            questions.append(
                {
                    "id": candidate.id,
                    "text": candidate.text,
                    "required": candidate.required,
                    "impact": (
                        "Локальный Python-аудит выявил неопределённость; затронуты требования: "
                        + ", ".join(candidate.requirement_ids)
                    ),
                    "options": candidate.options,
                }
            )
            represented_requirement_ids.update(candidate.requirement_ids)
            additions.append(candidate.id)
        return additions

    def preflight(self, project_id: str, focus: str = "") -> dict[str, Any]:
        """Run all free/local preparation before any paid API role."""
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            analysis = self.analytics.ensure(project_id)
            if analysis.dirty_clusters:
                self.analytics.analyze_evidence(project_id, analysis.dirty_clusters)
            prepared_analysis = self.analytics.ensure(project_id)
            request = self.store.read_artifact(project_id, "00-request.md")
            query = "\n\n".join(value for value in (request, focus.strip()) if value)
            route = self.sources.route(project.configuration, query)
            graph_context = json.loads(
                self.graph.project_context(project_id, query, project.revision)
            )
            modeler_available = (
                self.modeler.manifest_path.is_file()
                and self.modeler.search_index_path.is_file()
            )
            payload = {
                "schema_version": 1,
                "project_id": project_id,
                "revision": project.revision,
                "llm_calls": 0,
                "source_route": route.to_dict(),
                "graph_context_path": str(
                    self.store.project_dir(project_id)
                    / "agent_artifacts"
                    / f"graph-context-r{project.revision:03d}.json"
                ),
                "graph_status": graph_context.get("graph", {}),
                "modeler_available": modeler_available,
                "skill_runtime": self.prompts.runtime_plan(),
                "analysis": {
                    "requirements": len(prepared_analysis.requirements),
                    "evidence": len(prepared_analysis.evidence),
                    "gaps": len(prepared_analysis.gaps),
                    "acceptance_tests": len(prepared_analysis.acceptance_tests),
                    "dirty_clusters": prepared_analysis.dirty_clusters,
                    "schema_valid": prepared_analysis.schema_valid,
                    "modeler_passed": prepared_analysis.modeler_passed,
                },
            }
            target = (
                self.store.project_dir(project_id)
                / "agent_artifacts"
                / f"preflight-r{project.revision:03d}.json"
            )
            atomic_write_json(target, payload)
            self.store.append_event(
                project_id, "preflight_completed", {"artifact": target.name, "llm_calls": 0}
            )
            return {**payload, "path": str(target)}

    def save_answers(self, project_id: str, answers: dict[str, str]) -> Path:
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if project.status is not ProjectStatus.REQUIREMENTS_PENDING:
                raise WorkflowBlockedError("Ответы принимаются только на этапе требований.")
            questions = self.questions(project_id)
            self.analytics.synchronize_presented_questions(project_id, questions)
            known = {str(item.get("id")) for item in questions}
            unexpected = sorted(set(answers) - known)
            if unexpected:
                raise WorkflowBlockedError(f"Неизвестные идентификаторы вопросов: {unexpected}")
            path = self.store.project_dir(project_id) / "answers.json"
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            existing.update({key: value.strip() for key, value in answers.items()})
            atomic_write_json(path, existing)
            analysis_questions = {item.id for item in self.analytics.ensure(project_id).questions}
            for question_id, answer in answers.items():
                if question_id in analysis_questions:
                    decision_result = self.analytics.record_decision(project_id, question_id, answer)
                    if decision_result.get("recorded"):
                        project.revision = max(project.revision, int(decision_result["revision"]))
            self.store.save(project)
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
                answers_path = self.store.project_dir(project_id) / "answers.json"
                answers = json.loads(answers_path.read_text(encoding="utf-8"))
                synchronized = self.analytics.synchronize_presented_questions(
                    project_id, self.questions(project_id)
                )
                decided = {item.question_id for item in synchronized.decisions}
                unresolved: list[str] = []
                for question in synchronized.questions:
                    if question.id in decided:
                        continue
                    decision = self.analytics.record_decision(
                        project_id, question.id, str(answers.get(question.id, ""))
                    )
                    if decision.get("recorded"):
                        project.revision = max(project.revision, int(decision["revision"]))
                    else:
                        unresolved.append(question.id)
                if unresolved:
                    raise WorkflowBlockedError(
                        "Нужен более точный ответ на показанные вопросы: "
                        + ", ".join(unresolved)
                    )
                self.analytics.approve(project_id, "requirements")
                target = ProjectStatus.REQUIREMENTS_APPROVED
            elif stage == "design":
                self.analytics.approve(project_id, "design")
                target = ProjectStatus.DESIGN_APPROVED
            else:
                self._assert_instruction_verifiable(project_id)
                self._reconcile_upstream_analytical_approvals(project)
                self.analytics.approve(
                    project_id,
                    "instruction",
                    verified_instruction_artifact=True,
                )
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
            if stage == "instruction":
                approved_text = frontmatter.update(text, **values)
                answer_dir = self.store.project_dir(project_id) / "answers_md"
                atomic_write_text(
                    answer_dir / f"instruction-v{project.instruction_version:03d}-approved.md",
                    approved_text,
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

    def _reconcile_upstream_analytical_approvals(self, project: Project) -> None:
        """Repair legacy approval drift using approved artifacts and lifecycle state."""
        bundle = self.analytics.ensure(project.project_id)
        if bundle.revision != project.revision:
            raise WorkflowBlockedError(
                "Ревизия аналитической модели не совпадает с ревизией проекта."
            )
        restored: list[str] = []
        if bundle.requirements_approved_revision != bundle.revision:
            self.analytics.approve(project.project_id, "requirements")
            restored.append("requirements")
            bundle = self.analytics.load(project.project_id)
        if bundle.design_approved_revision != bundle.revision:
            self.analytics.approve(project.project_id, "design")
            restored.append("design")
        if restored:
            self.store.append_event(
                project.project_id,
                "analytical_approvals_reconciled",
                {
                    "revision": project.revision,
                    "restored": restored,
                    "basis": "approved upstream artifacts and feedback_pending lifecycle",
                },
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
            self.analytics.revoke_approvals(project_id, from_stage="instruction")
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
