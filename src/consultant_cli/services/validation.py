from __future__ import annotations

import json
import io
import runpy
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import ProjectStatus
from consultant_cli.infrastructure import frontmatter
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths


class ValidationService:
    def __init__(self, paths: RepositoryPaths, store: ProjectStore):
        self.paths = paths
        self.store = store

    def project(self, project_id: str) -> dict[str, Any]:
        project = self.store.load(project_id)
        directory = self.store.project_dir(project_id)
        errors: list[str] = []
        warnings: list[str] = []
        required = ["project.yaml", "00-request.md", "events.ndjson"]
        if project.requirements_version:
            required.append("01-requirements.md")
        if project.design_version:
            required.append("02-design.md")
        if project.instruction_version:
            required.append("03-instruction.md")
        for name in required:
            if not (directory / name).exists():
                errors.append(f"Отсутствует {name}")

        artifact_status = {}
        for name in ("01-requirements.md", "02-design.md", "03-instruction.md"):
            path = directory / name
            if path.exists():
                metadata, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
                artifact_status[name] = metadata.get("approval_status", "")

        if project.status in {
            ProjectStatus.DESIGN_PENDING,
            ProjectStatus.DESIGN_APPROVED,
            ProjectStatus.FEEDBACK_PENDING,
            ProjectStatus.SUCCESSFUL,
            ProjectStatus.NEEDS_REVISION,
        }:
            if artifact_status.get("01-requirements.md") != "approved":
                errors.append("Полный проект продолжен без approved требований")
        if project.instruction_version:
            if artifact_status.get("02-design.md") != "approved":
                errors.append("Инструкция создана без approved проекта и схемы")
        if project.status is ProjectStatus.SUCCESSFUL:
            if artifact_status.get("03-instruction.md") != "approved":
                errors.append("Статус successful не подтверждён в инструкции")
        if project.status is ProjectStatus.NEEDS_REVISION:
            metadata, _ = frontmatter.parse(
                (directory / "03-instruction.md").read_text(encoding="utf-8")
            ) if (directory / "03-instruction.md").exists() else ({}, "")
            if metadata.get("review_status") != "needs_revision":
                warnings.append("needs_revision не отражён во frontmatter инструкции")

        index_records = []
        if self.paths.examples_index.exists():
            index_records = [
                json.loads(line)
                for line in self.paths.examples_index.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        indexed = any(record.get("project_id") == project_id for record in index_records)
        if project.status is ProjectStatus.SUCCESSFUL and not indexed:
            errors.append("Успешный проект отсутствует в индексе примеров")
        if project.status is not ProjectStatus.SUCCESSFUL and indexed:
            errors.append("Неуспешный проект присутствует в индексе примеров")

        return {
            "project_id": project_id,
            "status": project.status.value,
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "artifacts": artifact_status,
        }

    def repository(self) -> dict[str, Any]:
        output = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(output), redirect_stderr(output):
                runpy.run_path(
                    str(self.paths.root / "scripts" / "validate_repository.py"),
                    run_name="__main__",
                )
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        except Exception as exc:
            exit_code = 7
            output.write(f"Validator failed: {exc}\n")
        return {
            "valid": exit_code == 0,
            "exit_code": exit_code,
            "output": output.getvalue().strip(),
        }
