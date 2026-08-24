from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import ProjectStatus
from consultant_cli.errors import InvalidConfigurationError, WorkflowBlockedError
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
            project = workflow.approve(
                project.project_id,
                "instruction",
                "Тестовый консультант",
                "Всё устраивает",
            )
            self.assertEqual(ProjectStatus.SUCCESSFUL, project.status)
            self.assertIn(project.project_id, paths.examples_index.read_text(encoding="utf-8"))

            project = workflow.request_changes(
                project.project_id, "Нужно уточнить один шаг"
            )
            self.assertEqual(ProjectStatus.NEEDS_REVISION, project.status)
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

    def test_instruction_approval_is_blocked_without_completed_evidence_gate(self):
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
            project, _ = self.advance_to_instruction(workflow, project.project_id)

            with self.assertRaisesRegex(WorkflowBlockedError, "нельзя перевести"):
                workflow.approve(
                    project.project_id,
                    "instruction",
                    "Консультант",
                    "Всё устраивает",
                )

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
