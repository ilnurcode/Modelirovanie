from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import Project, ProjectMode
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.generation import ArtifactRenderer
from consultant_cli.services.generation import PromptBuilder


class GenerationSchemaTest(unittest.TestCase):
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
            paths = RepositoryPaths(root)
            prompt = PromptBuilder(paths).build(
                Project("test", "Test", ProjectMode.FULL),
                "instruction",
                "User request",
                "Sources",
            )

            self.assertIn("POLICY_MARKER", prompt)
            self.assertIn("Полный режим", prompt)
            self.assertIn("results/test/01-requirements.md", prompt)
            self.assertIn("никогда не придумывать каталог", prompt)
            self.assertIn("не указывать `schema.json`", prompt)
            self.assertIn("дословно совпадать с id", prompt)

            self.assertIn("Не возвращайте пустые steps", prompt)
            self.assertIn("inferred заблокирует только финальный", prompt)


if __name__ == "__main__":
    unittest.main()
