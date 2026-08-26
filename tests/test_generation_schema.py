from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import ConfigurationInfo, Project, ProjectMode
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.generation import ArtifactRenderer, GenerationContract
from consultant_cli.services.generation import PromptBuilder
from consultant_cli.services.sources import SourceCandidate, SourceRoute
from tests.helpers import result


class GenerationSchemaTest(unittest.TestCase):
    @staticmethod
    def _route() -> SourceRoute:
        return SourceRoute(
            requested_product="Test",
            requested_release="1.0",
            compatibility="exact",
            use_xml=True,
            web_search_required=False,
            warnings=[],
        )

    def test_known_project_request_alias_is_normalized_deterministically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = root / "results" / "demo" / "00-request.md"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("request", encoding="utf-8")
            data = result("questions")
            data["sources"][0]["local_ref"] = "results/demo/request.md"
            project = Project("demo", "Demo", ProjectMode.FULL)

            repairs = GenerationContract(RepositoryPaths(root)).normalize_known_project_refs(
                data, project
            )

            self.assertEqual("results/demo/00-request.md", data["sources"][0]["local_ref"])
            self.assertEqual(1, len(repairs))

    def test_invented_local_alias_is_replaced_by_official_url(self):
        with tempfile.TemporaryDirectory() as temp:
            data = result("design")
            source = data["sources"][0]
            source["local_ref"] = "knowledge/articles/missing.md"
            source["url"] = "https://its.1c.ru/db/erp25doc"
            source["source_ref"] = source["local_ref"]

            repairs = GenerationContract(
                RepositoryPaths(Path(temp))
            ).normalize_missing_local_refs(data)

            self.assertEqual("", source["local_ref"])
            self.assertEqual(source["url"], source["source_ref"])
            self.assertEqual(1, len(repairs))

    def test_missing_official_url_is_restored_from_routed_local_article(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            article = root / "knowledge" / "articles" / "test.md"
            article.parent.mkdir(parents=True)
            article.write_text(
                "---\nid: s1\ntitle: Официальная статья\nproduct: 1C:ERP\n"
                "version: '2.5'\nverification_status: verified\n"
                "source_url: https://its.1c.ru/db/erp25doc/bookmark/Test\n---\n\n# Тест\n",
                encoding="utf-8",
            )
            data = result("design")
            data["sources"][0]["url"] = ""
            route = SourceRoute(
                requested_product="1С:ERP Управление предприятием 2",
                requested_release="2.5.27.49",
                compatibility="product_only",
                use_xml=False,
                external_docs_required=True,
                web_search_required=True,
                candidates=[
                    SourceCandidate(
                        ref="knowledge/articles/test.md",
                        title="Официальная статья",
                        score=10,
                        excerpt="",
                    )
                ],
            )
            contract = GenerationContract(RepositoryPaths(root))

            repairs = contract.normalize_required_official_url(data, route)
            contract.validate(
                data,
                "design",
                Project(
                    "test",
                    "Test",
                    ProjectMode.FULL,
                    configuration=ConfigurationInfo(
                        "1С:ERP Управление предприятием 2", "2.5", "2.5.27.49"
                    ),
                ),
                route,
            )

            self.assertEqual(
                "https://its.1c.ru/db/erp25doc/bookmark/Test",
                data["sources"][0]["url"],
            )
            self.assertEqual(1, len(repairs))

    def test_local_path_in_url_is_cleared_and_verified_its_is_canonicalized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            article = root / "knowledge" / "articles" / "test.md"
            article.parent.mkdir(parents=True)
            article.write_text("# Test\n", encoding="utf-8")
            data = result("design")
            source = data["sources"][0]
            source["url"] = source["local_ref"]
            source["source_ref"] = source["url"]
            source["url"] = "https://its.1c.ru/db/erp25doc/bookmark/Test"
            data["steps"][0]["verification_status"] = "verified_its"
            contract = GenerationContract(RepositoryPaths(root))

            status_repairs = contract.normalize_known_verification_statuses(data)
            self.assertEqual("verified", data["steps"][0]["verification_status"])
            self.assertEqual(1, len(status_repairs))

            source["url"] = source["local_ref"]
            url_repairs = contract.normalize_missing_local_refs(data)
            self.assertEqual("", source["url"])
            self.assertEqual(1, len(url_repairs))

    def test_verified_its_without_official_source_is_downgraded(self):
        data = result("design")
        data["steps"][0]["verification_status"] = "verified_its"

        repairs = GenerationContract.normalize_known_verification_statuses(data)

        self.assertEqual("inferred", data["steps"][0]["verification_status"])
        self.assertEqual(1, len(repairs))

    def test_unavailable_inferred_source_is_removed_from_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            data = result("design")
            data["sources"].append(
                {
                    "id": "missing-candidate",
                    "title": "Старый IFACE",
                    "local_ref": "IFACE/missing.md",
                    "url": "IFACE/missing.md",
                    "product": "1С:ERP",
                    "release": "",
                    "verification_status": "inferred",
                    "notes": "",
                    "source_ref": "C:/Users/Other/missing.md",
                    "node_id": "",
                    "edge_ids": [],
                }
            )
            data["steps"][0]["evidence_refs"].append("missing-candidate")
            contract = GenerationContract(RepositoryPaths(Path(temp)))

            repairs = contract.normalize_unavailable_inferred_sources(data)

            self.assertNotIn(
                "missing-candidate", {item["id"] for item in data["sources"]}
            )
            self.assertNotIn("missing-candidate", data["steps"][0]["evidence_refs"])
            self.assertEqual(1, len(repairs))

    def test_vanessa_feature_reuses_full_modeler_paths(self):
        data = result("design")
        data["vanessa_feature"] = (
            "Когда я открываю список «Продажи → НСИ продаж → Клиенты»"
        )

        repairs = GenerationContract.normalize_vanessa_ui_paths(
            data,
            [
                {
                    "from": "Продажи → НСИ продаж → Клиенты",
                    "to": "Продажи → Настройки и справочники → Клиенты",
                }
            ],
        )

        self.assertIn(
            "Продажи → Настройки и справочники → Клиенты",
            data["vanessa_feature"],
        )
        self.assertEqual(1, len(repairs))

    def test_project_query_allows_empty_document_flow_and_exact_modeler_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            graph = root / "1c_modeler_upgrade" / "graphs" / "route.json"
            graph.parent.mkdir(parents=True)
            graph.write_text("{}", encoding="utf-8")
            data = result("design")
            data["document_flow"] = []
            data["sources"][0].update(
                {
                    "id": "modeler-route:r1",
                    "local_ref": "1c_modeler_upgrade/graphs/route.json",
                    "verification_status": "verified_metadata",
                    "release": "2.5.27.49",
                }
            )
            data["steps"][0]["verification_status"] = "verified_metadata"
            data["steps"][0]["evidence_refs"] = ["modeler-route:r1"]
            route = SourceRoute(
                requested_product="1С:ERP",
                requested_release="2.5.27.49",
                compatibility="product_only",
                use_xml=False,
                web_search_required=False,
                warnings=[],
            )

            GenerationContract(RepositoryPaths(root)).validate(
                data,
                "design",
                Project(
                    "test",
                    "Test",
                    ProjectMode.FULL,
                    configuration=ConfigurationInfo(
                        "1С:ERP Управление предприятием 2", "2.5", "2.5.27.49"
                    ),
                ),
                route,
                allow_empty_document_flow=True,
            )

    def test_unbacked_metadata_step_is_downgraded_for_project_query(self):
        with tempfile.TemporaryDirectory() as temp:
            data = result("design")
            data["steps"][0]["verification_status"] = "verified_metadata"
            route = SourceRoute(
                requested_product="1С:ERP",
                requested_release="2.5.27.49",
                compatibility="product_only",
                use_xml=False,
                web_search_required=False,
                warnings=[],
            )
            project = Project("test", "Test", ProjectMode.FULL)
            project.configuration.release = "2.5.27.49"

            repairs = GenerationContract(
                RepositoryPaths(Path(temp))
            ).normalize_incompatible_metadata_steps(
                data, project, route, require_modeler_route=True
            )

            self.assertEqual("inferred", data["steps"][0]["verification_status"])
            self.assertEqual(1, len(repairs))

    def test_document_flow_is_first_content_block(self):
        project = Project("test", "Test", ProjectMode.FULL)
        rendered = ArtifactRenderer().instruction(project, result("instruction"))
        self.assertLess(
            rendered.index("## Общая последовательность документов"),
            rendered.index("## Краткий результат"),
        )
        self.assertIn("Документ Тест", rendered)

    def test_evidence_node_id_alias_is_normalized_to_source_id(self):
        with tempfile.TemporaryDirectory() as temp:
            data = result("design")
            data["sources"][0]["node_id"] = "Document.Test"
            data["document_flow"][0]["documents"][0]["node_id"] = "Document.Test"
            data["document_flow"][0]["documents"][0]["evidence_refs"] = [
                "Document.Test"
            ]

            repairs = GenerationContract(
                RepositoryPaths(Path(temp))
            ).normalize_evidence_refs(data)

            self.assertEqual(
                ["s1"], data["document_flow"][0]["documents"][0]["evidence_refs"]
            )
            self.assertEqual(1, len(repairs))

    def test_empty_document_refs_use_unique_matching_source_node(self):
        with tempfile.TemporaryDirectory() as temp:
            data = result("design")
            data["sources"][0]["node_id"] = "Document.Test"
            data["document_flow"][0]["documents"][0]["node_id"] = "Document.Test"
            data["document_flow"][0]["documents"][0]["evidence_refs"] = []

            GenerationContract(RepositoryPaths(Path(temp))).normalize_evidence_refs(data)

            self.assertEqual(
                ["s1"], data["document_flow"][0]["documents"][0]["evidence_refs"]
            )

    def test_same_real_action_is_allowed_in_different_design_steps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "knowledge" / "articles" / "test.md"
            source.parent.mkdir(parents=True)
            source.write_text("test", encoding="utf-8")
            data = result("design")
            data["steps"][0]["actions"] = ["Провести документ"]
            second = dict(data["steps"][0])
            second["id"] = "P02"
            second["title"] = "Провести второй документ"
            second["actions"] = ["Провести документ"]
            data["steps"].append(second)

            GenerationContract(RepositoryPaths(root)).validate(
                data,
                "design",
                Project("test", "Test", ProjectMode.FULL),
                self._route(),
            )

    def test_duplicate_action_inside_one_step_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "knowledge" / "articles" / "test.md"
            source.parent.mkdir(parents=True)
            source.write_text("test", encoding="utf-8")
            data = result("design")
            data["steps"][0]["actions"] = ["Провести документ", "Провести документ"]

            with self.assertRaisesRegex(Exception, "P01"):
                GenerationContract(RepositoryPaths(root)).validate(
                    data,
                    "design",
                    Project("test", "Test", ProjectMode.FULL),
                    self._route(),
                )

    def test_every_object_is_strict_and_requires_all_properties(self):
        path = Path(__file__).resolve().parents[1] / "schemas" / "generation-result.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))

        def visit(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    properties = set(node.get("properties", {}))
                    self.assertEqual(properties, set(node.get("required", [])))
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(schema)
        status_enums = []

        def collect_enums(node):
            if isinstance(node, dict):
                if node.get("enum") and "verified" in node["enum"]:
                    status_enums.append(set(node["enum"]))
                for value in node.values():
                    collect_enums(value)
            elif isinstance(node, list):
                for value in node:
                    collect_enums(value)

        collect_enums(schema)
        self.assertTrue(status_enums)
        self.assertTrue(all(values <= {"verified", "verified_metadata", "inferred"} for values in status_enums))

    def test_local_source_links_are_relative_to_project_directory(self):
        rendered = ArtifactRenderer._sources(
            [
                {
                    "id": "S1",
                    "title": "Local source",
                    "local_ref": "metadata/slices/procurement.md",
                    "verification_status": "verified_metadata",
                }
            ]
        )

        self.assertIn("(../../metadata/slices/procurement.md)", rendered)

    def test_prompt_includes_shared_modeling_policy_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "skills" / "write-1c-user-instruction"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("Instruction skill", encoding="utf-8")
            policy = (
                root
                / "skills"
                / "prepare-1c-consulting-answer"
                / "references"
                / "modeling-policy.md"
            )
            policy.parent.mkdir(parents=True)
            policy.write_text("POLICY_MARKER", encoding="utf-8")
            (policy.parent.parent / "SKILL.md").write_text(
                "ORCHESTRATOR_SKILL_MARKER", encoding="utf-8"
            )
            paths = RepositoryPaths(root)
            builder = PromptBuilder(paths)
            prompt = builder.build(
                Project("test", "Test", ProjectMode.FULL),
                "instruction",
                "User request",
                "Sources",
            )

            self.assertIn("POLICY_MARKER", prompt)
            self.assertIn("ORCHESTRATOR_SKILL_MARKER", prompt)
            self.assertEqual(
                "python_prompt_composition", builder.runtime_plan()["execution"]
            )
            self.assertIn("Полный режим", prompt)
            self.assertIn("results/test/01-requirements.md", prompt)
            self.assertIn("никогда не придумывать каталог", prompt)
            self.assertIn("не указывать `schema.json`", prompt)
            self.assertIn("дословно совпадать с id", prompt)

            self.assertIn("Один шаг — одна реальная", prompt)
            self.assertIn("запрещён статус inferred", prompt)
            self.assertIn("полным маршрутом из Modeler", prompt)
            self.assertIn("Продажи → Оптовые продажи → Заказы клиентов", prompt)

    def test_role_selects_kirill_stage_skill_even_for_saved_project_question(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, marker in (
                ("design-1c-process", "DESIGN_SKILL_MARKER"),
                ("write-1c-user-instruction", "WRITER_SKILL_MARKER"),
            ):
                target = root / "skills" / name / "SKILL.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(marker, encoding="utf-8")

            prompt = PromptBuilder(RepositoryPaths(root)).build(
                Project("test", "Test", ProjectMode.FULL),
                "design",
                "Куда нажать?",
                "Sources",
                role="instruction-writer",
            )

            self.assertIn("WRITER_SKILL_MARKER", prompt)
            self.assertNotIn("DESIGN_SKILL_MARKER", prompt)


if __name__ == "__main__":
    unittest.main()
