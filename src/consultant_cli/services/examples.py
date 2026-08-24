from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from consultant_cli.domain.models import ProjectStatus
from consultant_cli.errors import WorkflowBlockedError
from consultant_cli.infrastructure import frontmatter
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths, atomic_write_text


class ExampleRegistry:
    def __init__(self, paths: RepositoryPaths, store: ProjectStore):
        self.paths = paths
        self.store = store

    def rebuild(self) -> list[dict[str, Any]]:
        records = []
        for project in self.store.list():
            instruction = self.store.project_dir(project.project_id) / "03-instruction.md"
            if project.status is not ProjectStatus.SUCCESSFUL or not instruction.exists():
                continue
            metadata, _ = frontmatter.parse(instruction.read_text(encoding="utf-8"))
            if metadata.get("approval_status") != "approved" or metadata.get(
                "review_status"
            ) != "successful":
                continue
            approval = next(
                (
                    event
                    for event in reversed(self.store.read_events(project.project_id))
                    if event.get("type") == "instruction_approved"
                ),
                {},
            )
            if not approval:
                continue
            details = approval.get("details", {})
            records.append(
                {
                    "project_id": project.project_id,
                    "instruction_path": instruction.relative_to(self.paths.root).as_posix(),
                    "configuration": project.configuration.product,
                    "edition": project.configuration.edition,
                    "release": project.configuration.release,
                    "approved_at": approval.get("created_at", project.updated_at),
                    "approved_by": details.get("approved_by", "consultant"),
                    "status": "successful",
                }
            )
        records.sort(key=lambda item: (item["configuration"], item["release"], item["project_id"]))
        text = "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        )
        atomic_write_text(self.paths.examples_index, text + ("\n" if text else ""))
        return records

    def promote(self, project_id: str) -> dict[str, Any]:
        project = self.store.load(project_id)
        if project.status is not ProjectStatus.SUCCESSFUL:
            raise WorkflowBlockedError(
                "Только явно подтверждённая инструкция может стать примером."
            )
        records = self.rebuild()
        return next(record for record in records if record["project_id"] == project_id)

    def compatible(self, product: str, release: str) -> list[dict[str, Any]]:
        # Rebuild on every read so a revoked, manually changed or deleted project can
        # never remain available as a stale example.
        records = self.rebuild()
        exact = [
            record
            for record in records
            if record.get("configuration", "").casefold() == product.casefold()
            and record.get("release") == release
        ]
        return exact
