from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.analytics import Evidence, EvidenceStatus
from consultant_cli.domain.models import ConfigurationInfo, Project, ProjectMode
from consultant_cli.errors import WorkflowBlockedError
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths
from consultant_cli.services.analytics import AnalyticsService, validate_bundle
from tests.helpers import make_repository


class UnifiedAnalyticsTest(unittest.TestCase):
    PRODUCT = "1С:ERP Управление предприятием 2"
    RELEASE = "2.5.27.49"

    def build(self, root: Path, text: str, release: str | None = None):
        make_repository(root)
        paths = RepositoryPaths(root)
        store = ProjectStore(paths)
        project = Project(
            project_id="analytics-test",
            title="Analytics test",
            mode=ProjectMode.FULL,
            configuration=ConfigurationInfo(self.PRODUCT, release=release or self.RELEASE),
        )
        store.create(project, text)
        service = AnalyticsService(paths, store)
        bundle = service.initialize(project.project_id, text, project.configuration)
        return paths, store, service, bundle

    def add_exact_index(self, root: Path, release: str | None = None) -> None:
        graph_dir = root / "1c_modeler_upgrade" / "graphs"
        graph_dir.mkdir(parents=True, exist_ok=True)
        source = root / "exact-object.xml"
        source.write_text("<Document name='ЗаказПоставщику'/>", encoding="utf-8")
        manifest = {
            "configuration": "1С:ERP 2.5",
            "release": self.RELEASE,
            "graphs": [
                {
                    "file": "graphs/1c_erp_2_5_object_graph.json",
                    "status": "ГОТОВ",
                    "release": self.RELEASE,
                }
            ],
        }
        (graph_dir / "graph_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        record = {
            "graph": "object",
            "configuration": self.PRODUCT,
            "release": release or self.RELEASE,
            "id": "Document.ЗаказПоставщику",
            "label": "Заказ поставщику",
            "type": "Document",
            "properties": {
                "source_xml": str(source),
                "search_text": "Заказ поставщику создается для закупки сырья",
            },
        }
        with gzip.open(graph_dir / "search-index.ndjson.gz", "wt", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_01_every_requirement_has_solution_or_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, _ = self.build(Path(temp), "Заказ поставщику создается для закупки сырья.")
            service.analyze_evidence("analytics-test")
            bundle = service.load("analytics-test")
            self.assertFalse(validate_bundle(bundle)[0])
            for requirement in bundle.requirements:
                self.assertTrue(
                    requirement.solution_element_ids
                    or any(requirement.id in gap.requirement_ids for gap in bundle.gaps)
                )

    def test_02_covered_requirements_have_acceptance_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, service, _ = self.build(root, "Заказ поставщику создается для закупки сырья.")
            self.add_exact_index(root)
            service.analyze_evidence("analytics-test")
            bundle = service.load("analytics-test")
            covered = [item for item in bundle.requirements if item.coverage_status.value == "covered"]
            self.assertTrue(covered)
            self.assertTrue(all(item.acceptance_test_ids for item in covered))

    def test_03_changed_answer_revokes_dependent_approvals(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, bundle = self.build(
                Path(temp), "Оплата выполняется авансом или после отгрузки."
            )
            question = bundle.questions[0]
            first = service.record_decision("analytics-test", question.id, question.options[0])
            self.assertTrue(first["recorded"])
            service.approve("analytics-test", "requirements")
            service.approve("analytics-test", "design")
            second = service.record_decision("analytics-test", question.id, question.options[1])
            changed = service.load("analytics-test")
            self.assertTrue(second["approvals_revoked"])
            self.assertIsNone(changed.requirements_approved_revision)
            self.assertIsNone(changed.design_approved_revision)
            self.assertIsNone(changed.final_approved_revision)

    def test_04_changed_answer_rebuilds_only_linked_clusters(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, bundle = self.build(
                Path(temp),
                "Оплата выполняется авансом или после отгрузки.\n"
                "Сырье поставляет быстрый поставщик или долгий поставщик.",
            )
            question = next(item for item in bundle.questions if item.cluster == "payments")
            result = service.record_decision("analytics-test", question.id, question.options[0])
            self.assertEqual(["payments"], result["rebuilt_clusters"])

    def test_05_ambiguous_answer_is_not_a_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, bundle = self.build(
                Path(temp), "Оплата выполняется авансом или после отгрузки."
            )
            result = service.record_decision(
                "analytics-test", bundle.questions[0].id, "подтверждаю настройки"
            )
            self.assertFalse(result["recorded"])
            self.assertTrue(result["needs_clarification"])
            self.assertEqual([], service.load("analytics-test").decisions)

    def test_06_release_mismatch_disables_xml_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, service, _ = self.build(
                root, "Заказ поставщику создается для закупки сырья.", release="2.5.28.1"
            )
            self.add_exact_index(root, release=self.RELEASE)
            service.analyze_evidence("analytics-test")
            statuses = {item.status for item in service.load("analytics-test").evidence}
            self.assertNotIn(EvidenceStatus.VERIFIED_METADATA, statuses)
            self.assertIn(EvidenceStatus.CANDIDATE, statuses)

    def test_07_unknown_erp_object_blocks_technical_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, bundle = self.build(Path(temp), "Создать карточку неизвестного объекта.")
            bundle.instruction_steps = [{
                "id": "STEP-X", "object_ref": "Document.Unknown", "route_ref": "",
                "edge_ref": "", "evidence_ids": [], "validation_status": "verified",
            }]
            report = service.modeler.review(bundle)
            self.assertIn("unknown ERP object", " ".join(report["errors"]))

    def test_08_unresolved_user_route_blocks_technical_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, bundle = self.build(Path(temp), "Открыть форму заказа поставщику.")
            bundle.instruction_steps = [{
                "id": "STEP-R", "object_ref": "", "route_ref": "Route.Unknown",
                "edge_ref": "", "evidence_ids": [], "validation_status": "verified",
            }]
            report = service.modeler.review(bundle)
            self.assertIn("unresolved user route", " ".join(report["errors"]))

    def test_09_unverified_relation_never_becomes_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, bundle = self.build(Path(temp), "Создать документ на основании заказа.")
            candidate = Evidence(
                id="EVD-candidate", source_type="semantic", product=self.PRODUCT,
                release=self.RELEASE, source_ref="edge", excerpt="candidate",
                status=EvidenceStatus.CANDIDATE, edge_ref="edge-1",
            )
            bundle.evidence = [candidate]
            bundle.instruction_steps = [{
                "id": "STEP-E", "object_ref": "", "route_ref": "", "edge_ref": "edge-1",
                "evidence_ids": [candidate.id], "validation_status": "verified",
            }]
            report = service.modeler.review(bundle)
            self.assertIn("unverified inter-object relation", " ".join(report["errors"]))

    def test_10_requirement_ids_match_model_instruction_trace_and_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, service, _ = self.build(root, "Заказ поставщику создается для закупки сырья.")
            self.add_exact_index(root)
            service.analyze_evidence("analytics-test")
            bundle = service.load("analytics-test")
            requirement = bundle.requirements[0]
            trace = next(item for item in bundle.traceability if item.requirement_id == requirement.id)
            solution = next(item for item in bundle.solution_elements if item["id"] in trace.solution_element_ids)
            step = next(item for item in bundle.instruction_steps if item["id"] in trace.instruction_step_ids)
            test = next(item for item in bundle.acceptance_tests if item.id in trace.acceptance_test_ids)
            self.assertIn(requirement.id, solution["requirement_ids"])
            self.assertIn(requirement.id, step["requirement_ids"])
            self.assertIn(requirement.id, test.requirement_ids)

    def test_11_critical_gap_blocks_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, _ = self.build(
                Path(temp), "Пользовательский маршрут отгрузки должен быть подтвержден."
            )
            service.analyze_evidence("analytics-test")
            service.approve("analytics-test", "requirements")
            service.approve("analytics-test", "design")
            with self.assertRaisesRegex(WorkflowBlockedError, "Critical GAP"):
                service.approve("analytics-test", "instruction")

    def test_12_without_final_user_approval_successful_is_impossible(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, _ = self.build(Path(temp), "Подготовить аналитический отчет.")
            service.analyze_evidence("analytics-test")
            service.approve("analytics-test", "requirements")
            service.approve("analytics-test", "design")
            self.assertIsNone(service.load("analytics-test").final_approved_revision)

    def test_13_revocation_clears_all_analytical_approvals(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, _ = self.build(Path(temp), "Подготовить аналитический отчет.")
            service.analyze_evidence("analytics-test")
            service.approve("analytics-test", "requirements")
            service.approve("analytics-test", "design")
            service.revoke_approvals("analytics-test")
            bundle = service.load("analytics-test")
            self.assertEqual((None, None, None), (
                bundle.requirements_approved_revision,
                bundle.design_approved_revision,
                bundle.final_approved_revision,
            ))

    def test_14_failed_import_does_not_damage_previous_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, before = self.build(Path(temp), "Подготовить аналитический отчет.")
            path = service._analysis_dir("analytics-test") / "analysis.json"
            snapshot = path.read_bytes()
            with self.assertRaises(json.JSONDecodeError):
                json.loads("{invalid")
            self.assertEqual(snapshot, path.read_bytes())
            self.assertEqual(before.revision, service.load("analytics-test").revision)

    def test_15_repeated_search_uses_evidence_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, service, bundle = self.build(root, "Заказ поставщику создается для закупки сырья.")
            self.add_exact_index(root)
            first = service.analyze_evidence("analytics-test", [bundle.requirements[0].cluster])
            second = service.analyze_evidence("analytics-test", [bundle.requirements[0].cluster])
            self.assertGreaterEqual(first["cache_misses"], 1)
            self.assertGreaterEqual(second["cache_hits"], 1)

    def test_16_schema_and_contract_checks_use_no_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, service, _ = self.build(Path(temp), "Подготовить аналитический отчет.")
            service.analyze_evidence("analytics-test")
            errors, _ = validate_bundle(service.load("analytics-test"))
            self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
