from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import ProjectStatus
from consultant_cli.errors import GenerationValidationError, InvalidConfigurationError, WorkflowBlockedError
from consultant_cli.infrastructure.settings import AppSettings
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths
from consultant_cli.services.workflow import WorkflowService
from tests.helpers import FakeAgents, make_repository, result


def add_exact_modeler_route(root: Path) -> None:
    graph_dir = root / "1c_modeler_upgrade" / "graphs"
    graph_dir.mkdir(parents=True)
    source = root / "modeler-route-source.md"
    source.write_text("Раздел → Группа → Команда", encoding="utf-8")
    (graph_dir / "graph_manifest.json").write_text(
        json.dumps({"configuration": "1С:ERP 2.5", "graphs": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (graph_dir / "1c_erp_2_5_route_graph.json").write_text(
        json.dumps(
            {
                "configuration": "1С:ERP 2.5",
                "release": "2.5.27.49",
                "nodes": {
                    "r1": {
                        "id": "r1",
                        "label": "Команда",
                        "properties": {
                            "path": "Раздел → Группа → Команда",
                            "source_path": str(source),
                        },
                    }
                },
                "edges": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (graph_dir / "1c_erp_2_5_semantic_graph.json").write_text(
        json.dumps({"status": "ГОТОВ", "nodes": {}, "edges": []}, ensure_ascii=False),
        encoding="utf-8",
    )


class WorkflowTest(unittest.TestCase):
    def build(self, root: Path, responses: list[dict]):
        make_repository(root)
        paths = RepositoryPaths(root)
        store = ProjectStore(paths)
        settings = AppSettings(default_agent="fake")
        workflow = WorkflowService(paths, store, settings, FakeAgents(responses))
        return paths, store, workflow

    def test_existing_unicode_project_id_remains_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, store, _workflow = self.build(root, [])
            legacy = paths.results / "старый-проект"
            legacy.mkdir()
            self.assertEqual(legacy.resolve(), store.project_dir("старый-проект"))
            with self.assertRaises(Exception):
                store.project_dir("..\\escape")

    def test_preflight_bootstraps_missing_legacy_analysis_without_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, store, workflow = self.build(root, [])
            project = workflow.create_project(
                title="Старый проект",
                prompt="Описать производство и отгрузку",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            project.revision = 7
            store.save(project)
            analysis_path = paths.results / project.project_id / "analysis" / "analysis.json"
            analysis_path.unlink()

            result_payload = workflow.preflight(project.project_id)

            self.assertEqual(0, result_payload["llm_calls"])
            self.assertEqual(7, workflow.analytics.load(project.project_id).revision)
            self.assertTrue(analysis_path.is_file())
            self.assertEqual(
                "python_prompt_composition",
                result_payload["skill_runtime"]["execution"],
            )
            self.assertTrue(
                any(
                    item["type"] == "legacy_analysis_initialized"
                    for item in store.read_events(project.project_id)
                )
            )

    def advance_to_instruction(self, workflow: WorkflowService, project_id: str):
        project, _ = workflow.run(project_id)
        self.assertEqual(ProjectStatus.REQUIREMENTS_PENDING, project.status)
        workflow.save_answers(project_id, {"Q1": "До поступления"})
        workflow.approve(
            project_id, "requirements", "Консультант", "Требования утверждаю"
        )
        project, _ = workflow.run(project_id)
        self.assertEqual(ProjectStatus.DESIGN_PENDING, project.status)
        workflow.approve(project_id, "design", "Консультант", "Проект утверждаю")
        return workflow.run(project_id)

    def test_full_approval_and_late_revocation(self):
        with tempfile.TemporaryDirectory() as temp:
            paths, store, workflow = self.build(
                Path(temp), [result("questions"), result("design"), result("instruction")]
            )
            add_exact_modeler_route(Path(temp))
            project = workflow.create_project(
                title="Полный процесс",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            project, artifact = self.advance_to_instruction(
                workflow, project.project_id
            )
            self.assertEqual(ProjectStatus.FEEDBACK_PENDING, project.status)
            self.assertTrue(artifact.exists())
            self.assertEqual([], workflow.examples.rebuild())
            # Simulate approval drift created by legacy instruction feedback:
            # approved Markdown/lifecycle stayed valid while analytics was reset.
            workflow.analytics.revoke_approvals(project.project_id)
            project = workflow.approve(
                project.project_id,
                "instruction",
                "Тестовый консультант",
                "Всё устраивает",
            )
            self.assertEqual(ProjectStatus.SUCCESSFUL, project.status)
            reconciled = workflow.analytics.load(project.project_id)
            self.assertEqual(reconciled.revision, reconciled.design_approved_revision)
            self.assertTrue(
                any(
                    event["type"] == "analytical_approvals_reconciled"
                    for event in store.read_events(project.project_id)
                )
            )
            self.assertIn(project.project_id, paths.examples_index.read_text(encoding="utf-8"))

            project = workflow.request_changes(
                project.project_id, "Нужно уточнить один шаг"
            )
            self.assertEqual(ProjectStatus.NEEDS_REVISION, project.status)
            after_feedback = workflow.analytics.load(project.project_id)
            self.assertEqual(after_feedback.revision, after_feedback.requirements_approved_revision)
            self.assertEqual(after_feedback.revision, after_feedback.design_approved_revision)
            self.assertIsNone(after_feedback.final_approved_revision)
            self.assertNotIn(project.project_id, paths.examples_index.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(store.read_events(project.project_id)), 5)

    def test_project_title_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, workflow = self.build(Path(temp), [])
            with self.assertRaisesRegex(
                InvalidConfigurationError, "Название проекта обязательно"
            ):
                workflow.create_project(
                    title="   ",
                    prompt="Создать инструкцию",
                    mode="full",
                    product="not_configured",
                )

    def test_markdown_source_is_preserved_in_project(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, workflow = self.build(Path(temp), [])
            project = workflow.create_project(
                title="Проект из файла",
                prompt="# Полное ТЗ\n\nТребование.",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
                source_name="tz.md",
                source_bytes=b"# Full TZ\r\n\r\nRequirement.\r\n",
            )
            directory = store.project_dir(project.project_id)
            self.assertEqual(
                b"# Full TZ\r\n\r\nRequirement.\r\n",
                (directory / "00-source.md").read_bytes(),
            )
            self.assertTrue(
                any(
                    event["type"] == "project_created_from_markdown"
                    and event["details"]["source_name"] == "tz.md"
                    for event in store.read_events(project.project_id)
                )
            )

    def test_question_stage_does_not_scan_modeler_index(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, workflow = self.build(Path(temp), [result("questions")])
            project = workflow.create_project(
                title="Вопросы без Modeler",
                prompt="Уточнить вариант закупки или производства",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            workflow.modeler.context = lambda *_args, **_kwargs: self.fail(
                "Modeler context must not run for translator"
            )

            generated, _ = workflow.run(project.project_id)

            self.assertEqual(ProjectStatus.REQUIREMENTS_PENDING, generated.status)

    def test_question_result_contains_coverage_prompt_and_token_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, workflow = self.build(Path(temp), [result("questions")])
            workflow.agents.last_usage = {
                "input_tokens": 120,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_tokens": 4,
            }
            project = workflow.create_project(
                title="Прозрачный вызов",
                prompt="Уточнить вариант закупки или производства",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )

            generated, artifact = workflow.run(project.project_id)

            self.assertEqual("hybrid", generated.generation.deliverable)
            markdown = artifact.read_text(encoding="utf-8")
            self.assertIn("## Аудит полноты вопросов", markdown)
            self.assertIn("это не фиксированный лимит", markdown)
            self.assertIn("## Выполнение AI", markdown)
            self.assertIn("Всего токенов (input + output): **150**", markdown)
            artifacts = store.project_dir(project.project_id) / "agent_artifacts"
            prompt_path = artifacts / "questions-r001-a001-prompt.txt"
            execution_path = artifacts / "questions-r001-a001-execution.json"
            self.assertIn(
                "Уточнить вариант закупки или производства",
                prompt_path.read_text(encoding="utf-8"),
            )
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            self.assertEqual(150, execution["total_tokens"])
            self.assertEqual("completed", execution["status"])

    def test_presented_questions_and_freeform_answers_are_used_for_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, workflow = self.build(Path(temp), [result("questions")])
            project = workflow.create_project(
                title="Канонические вопросы",
                prompt="Уточнить вариант закупки или производства",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            workflow.run(project.project_id)
            presented = workflow.analytics.load(project.project_id).questions
            self.assertEqual("Q1", presented[0].id)
            self.assertEqual(2, len(presented))

            workflow.save_answers(
                project.project_id,
                {item.id: "Собственный точный вариант" for item in presented},
            )
            approved = workflow.approve(
                project.project_id,
                "requirements",
                "Пользователь Herdr/Pi",
                "Все показанные вопросы отвечены; требования утверждены",
            )

            self.assertEqual(ProjectStatus.REQUIREMENTS_APPROVED, approved.status)
            decisions = workflow.analytics.load(project.project_id).decisions
            self.assertEqual("Собственный точный вариант", decisions[0].exact_user_answer)

    def test_legacy_mode_cannot_be_selected_for_new_project(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, workflow = self.build(Path(temp), [])
            with self.assertRaisesRegex(InvalidConfigurationError, "только полный режим"):
                workflow.create_project(
                    title="Старый режим",
                    prompt="Создать инструкцию",
                    mode="flexible",
                    product="not_configured",
                )

    def test_instruction_generation_rejects_inferred_operational_steps(self):
        with tempfile.TemporaryDirectory() as temp:
            generated = result("instruction")
            generated["steps"][0]["verification_status"] = "inferred"
            _, _, workflow = self.build(
                Path(temp), [result("questions"), result("design"), generated]
            )
            project = workflow.create_project(
                title="Неподтверждённый процесс",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            with self.assertRaisesRegex(GenerationValidationError, "inferred"):
                self.advance_to_instruction(workflow, project.project_id)

    def test_deleted_project_leaves_list_and_confirmed_examples(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, store, workflow = self.build(
                root, [result("questions"), result("design"), result("instruction")]
            )
            add_exact_modeler_route(root)
            project = workflow.create_project(
                title="Проект для удаления",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            project, _ = self.advance_to_instruction(workflow, project.project_id)
            project = workflow.approve(
                project.project_id,
                "instruction",
                "Консультант",
                "Всё устраивает",
            )
            self.assertIn(project.project_id, paths.examples_index.read_text(encoding="utf-8"))

            trash_path = workflow.delete_project(project.project_id)

            self.assertTrue(trash_path.is_dir())
            self.assertEqual([], store.list())
            self.assertNotIn(project.project_id, paths.examples_index.read_text(encoding="utf-8"))
            self.assertFalse(store.project_dir(project.project_id).exists())

    def test_full_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, workflow = self.build(
                Path(temp), [result("questions"), result("design"), result("instruction")]
            )
            project = workflow.create_project(
                title="Полный процесс",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            project, _ = workflow.run(project.project_id)
            self.assertEqual(ProjectStatus.REQUIREMENTS_PENDING, project.status)
            with self.assertRaises(WorkflowBlockedError):
                workflow.approve(
                    project.project_id,
                    "requirements",
                    "Консультант",
                    "Требования утверждаю",
                )
            workflow.save_answers(project.project_id, {"Q1": "До поступления"})
            project = workflow.approve(
                project.project_id, "requirements", "Консультант", "Требования утверждаю"
            )
            self.assertEqual(ProjectStatus.REQUIREMENTS_APPROVED, project.status)

            project, _ = workflow.run(project.project_id)
            self.assertEqual(ProjectStatus.DESIGN_PENDING, project.status)
            project = workflow.approve(
                project.project_id, "design", "Консультант", "Проект утверждаю"
            )
            self.assertEqual(ProjectStatus.DESIGN_APPROVED, project.status)

            project, _ = workflow.run(project.project_id)
            self.assertEqual(ProjectStatus.FEEDBACK_PENDING, project.status)
            self.assertTrue((store.project_dir(project.project_id) / "03-instruction.md").exists())
            self.assertTrue(
                (store.project_dir(project.project_id) / "answers_md" / "instruction-v001-draft.md").exists()
            )
            self.assertTrue(
                (store.project_dir(project.project_id) / "03-instruction-validation.md").exists()
            )
            self.assertTrue(
                (store.project_dir(project.project_id) / "03-modeler-review.md").exists()
            )

    def test_failed_generation_restores_approved_gate_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            invalid_design = result("design")
            invalid_design["sources"][0]["local_ref"] = "missing-source.md"
            _, store, workflow = self.build(
                Path(temp), [result("questions"), invalid_design]
            )
            project = workflow.create_project(
                title="Повтор генерации",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            workflow.run(project.project_id)
            workflow.save_answers(project.project_id, {"Q1": "До поступления"})
            workflow.approve(
                project.project_id,
                "requirements",
                "Консультант",
                "Требования утверждаю",
            )

            with self.assertRaises(Exception):
                workflow.run(project.project_id)

            restored = store.load(project.project_id)
            self.assertEqual(ProjectStatus.REQUIREMENTS_APPROVED, restored.status)
            self.assertIn("missing-source.md", restored.last_error)
            raw_results = list(
                (store.project_dir(project.project_id) / "agent_artifacts").glob(
                    "design-raw-*.json"
                )
            )
            self.assertEqual(1, len(raw_results))
            self.assertEqual(
                "missing-source.md",
                json.loads(raw_results[0].read_text(encoding="utf-8"))["sources"][0][
                    "local_ref"
                ],
            )

    def test_saved_raw_generation_is_recovered_without_another_agent_call(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, workflow = self.build(Path(temp), [result("questions")])
            project = workflow.create_project(
                title="Восстановление ответа",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            workflow.run(project.project_id)
            workflow.save_answers(project.project_id, {"Q1": "До поступления"})
            project = workflow.approve(
                project.project_id,
                "requirements",
                "Консультант",
                "Требования утверждаю",
            )
            raw_path = (
                store.project_dir(project.project_id)
                / "agent_artifacts"
                / f"design-raw-r{project.revision:03d}-a001.json"
            )
            raw_path.write_text(
                json.dumps(result("design"), ensure_ascii=False), encoding="utf-8"
            )

            recovered, artifact = workflow.recover_latest_generation(project.project_id)

            self.assertEqual(ProjectStatus.DESIGN_PENDING, recovered.status)
            self.assertEqual(1, recovered.design_version)
            self.assertTrue(artifact.is_file())

            recovered_again, _ = workflow.recover_latest_generation(project.project_id)
            self.assertEqual(ProjectStatus.DESIGN_PENDING, recovered_again.status)
            self.assertEqual(1, recovered_again.design_version)
            self.assertTrue(
                any(
                    event["type"] == "design_recovered_from_raw"
                    and event["details"]["api_calls"] == 0
                    for event in store.read_events(project.project_id)
                )
            )

    def test_pending_instruction_is_recovered_without_new_version_or_api_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, store, workflow = self.build(
                root, [result("questions"), result("design"), result("instruction")]
            )
            add_exact_modeler_route(root)
            project = workflow.create_project(
                title="Восстановление инструкции",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            pending, _ = self.advance_to_instruction(workflow, project.project_id)
            self.assertEqual(ProjectStatus.FEEDBACK_PENDING, pending.status)
            self.assertEqual(1, pending.instruction_version)

            recovered, artifact = workflow.recover_latest_generation(project.project_id)

            self.assertEqual(ProjectStatus.FEEDBACK_PENDING, recovered.status)
            self.assertEqual(1, recovered.instruction_version)
            self.assertTrue(artifact.is_file())
            report = json.loads(
                store.read_artifact(project.project_id, "03-modeler-review.json")
            )
            self.assertEqual("passed", report["verdict"])
            self.assertTrue(
                any(
                    event["type"] == "instruction_recovered_from_raw"
                    and event["details"]["api_calls"] == 0
                    for event in store.read_events(project.project_id)
                )
            )

    def test_failed_project_query_raw_is_reused_and_repaired_without_api(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, workflow = self.build(Path(temp), [])
            project = workflow.create_project(
                title="Вопрос по проекту",
                prompt="Настроить продажи",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            question = "Как включить продажи?"
            kind = "process"
            query_id = hashlib.sha256(
                f"{project.revision}\0{kind}\0{question}".encode("utf-8")
            ).hexdigest()[:12]
            raw = result("design")
            raw["sources"][0]["local_ref"] = "knowledge/articles/invented.md"
            raw["sources"][0]["url"] = "https://its.1c.ru/db/erp25doc"
            raw["sources"][0]["source_ref"] = raw["sources"][0]["local_ref"]
            answers_dir = store.project_dir(project.project_id) / "answers_md"
            answers_dir.mkdir(parents=True, exist_ok=True)
            (answers_dir / f"query-{query_id}-raw.json").write_text(
                json.dumps(raw, ensure_ascii=False), encoding="utf-8"
            )
            workflow.agents.role_profile = lambda _role: workflow.agents.profile
            workflow.agents.generate_role = lambda *_args, **_kwargs: self.fail(
                "saved query raw must be reused"
            )

            answer = workflow.ask_project(project.project_id, question, kind)

            self.assertTrue(answer["reused"])
            saved = json.loads(
                (answers_dir / f"query-{query_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual("", saved["sources"][0]["local_ref"])
            self.assertEqual(
                "https://its.1c.ru/db/erp25doc",
                saved["sources"][0]["source_ref"],
            )

    def test_pending_design_can_be_revised_without_revoking_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, workflow = self.build(
                Path(temp), [result("questions"), result("design"), result("design")]
            )
            project = workflow.create_project(
                title="Доработка схемы",
                prompt="Создай процесс закупки",
                mode="full",
                product="1С:ERP Управление предприятием 2",
                release="2.5.27.49",
            )
            workflow.run(project.project_id)
            workflow.save_answers(project.project_id, {"Q1": "До поступления"})
            workflow.approve(
                project.project_id,
                "requirements",
                "Консультант",
                "Требования утверждаю",
            )
            first, _ = workflow.run(project.project_id)
            self.assertEqual(1, first.design_version)

            returned = workflow.revise_design(
                project.project_id, "Использовать точный XML-срез"
            )
            self.assertEqual(ProjectStatus.REQUIREMENTS_APPROVED, returned.status)
            self.assertEqual(1, returned.design_version)

            revised, _ = workflow.run(project.project_id)
            self.assertEqual(ProjectStatus.DESIGN_PENDING, revised.status)
            self.assertEqual(2, revised.design_version)
            self.assertIn(
                "Использовать точный XML-срез",
                store.read_artifact(project.project_id, "feedback.md"),
            )


if __name__ == "__main__":
    unittest.main()
