from __future__ import annotations

import json
from pathlib import Path

from consultant_cli.infrastructure.settings import AgentProfile


def make_repository(root: Path) -> None:
    (root / "skills").mkdir(parents=True)
    (root / "README.md").write_text("# Test repo\n", encoding="utf-8")
    for skill in (
        "analyze-1c-requirements",
        "design-1c-process",
        "write-1c-user-instruction",
    ):
        path = root / "skills" / skill
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: test\n---\n\n# Test\n",
            encoding="utf-8",
        )
    (root / "schemas").mkdir()
    source_schema = (
        Path(__file__).resolve().parents[1] / "schemas" / "generation-result.schema.json"
    )
    (root / "schemas" / "generation-result.schema.json").write_text(
        source_schema.read_text(encoding="utf-8"), encoding="utf-8"
    )
    article = root / "knowledge" / "articles" / "test.md"
    article.parent.mkdir(parents=True)
    article.write_text("# Тестовая статья\n\nЗакупка и настройка процесса.\n", encoding="utf-8")
    (root / "processes").mkdir()
    metadata = root / "metadata" / "index"
    metadata.mkdir(parents=True)
    (metadata / "configuration.json").write_text(
        json.dumps(
            {
                "synonym": "1С:ERP Управление предприятием 2",
                "version": "2.5.27.49",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "metadata" / "slices").mkdir()
    (root / "examples" / "approved").mkdir(parents=True)
    (root / "results").mkdir()


def result(artifact_type: str) -> dict:
    base = {
        "artifact_type": artifact_type,
        "title": "Тестовый процесс",
        "summary": "Проверяемый результат.",
        "document_flow": [],
        "vanessa_feature": "",
        "sources": [
            {
                "id": "s1",
                "title": "Тестовая статья",
                "local_ref": "knowledge/articles/test.md",
                "verification_status": "verified",
                "source_ref": "knowledge/articles/test.md",
                "node_id": "",
                "edge_ids": [],
            }
        ],
    }
    if artifact_type == "questions":
        base["questions"] = [
            {
                "id": "Q1",
                "text": "Какова граница процесса?",
                "required": True,
                "impact": "Определяет конечную точку.",
                "options": ["До заказа", "До поступления"],
            }
        ]
        return base
    base.update(
        {
            "document_flow": [
                {
                    "title": "Основной маршрут",
                    "condition": "Типовой вариант",
                    "documents": [
                        {"name": "Документ Тест", "node_id": "test-node", "evidence_refs": ["s1"]}
                    ],
                }
            ],
            "vanessa_feature": "",
            "implementation": ["Использовать типовой маршрут"],
            "roles": ["Консультант"],
            "objects": ["Тестовый объект"],
            "settings": ["Включить тестовую настройку"],
            "diagram_mermaid": "flowchart LR\n A[Начало] --> B[Результат]",
            "steps": [
                {
                    "id": "P01",
                    "title": "Выполнить действие",
                    "role": "Консультант",
                    "precondition": "Настройки выполнены",
                    "ui_path": "Раздел → Группа → Команда",
                    "form": "Тестовая форма",
                    "fields": ["Поле — значение"],
                    "command": "Провести",
                    "actions": ["Открыть раздел", "Проверить результат"],
                    "expected_status": "Проведён",
                    "result": "Результат получен",
                    "verification": "Открыть созданный объект",
                    "verification_status": "verified",
                    "evidence_refs": ["s1"],
                }
            ],
            "alternatives": ["Альтернативный маршрут"],
            "limitations": ["Тестовое ограничение"],
            "customizations": ["Доработка не требуется"],
        }
    )
    return base


class FakeAgents:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.profile = AgentProfile(name="fake", kind="custom_cli", command="fake")

    def get_profile(self, name=None):
        del name
        return self.profile

    def generate(self, profile, prompt, schema, allow_web_search=False):
        del profile, prompt, schema, allow_web_search
        return self.responses.pop(0)
