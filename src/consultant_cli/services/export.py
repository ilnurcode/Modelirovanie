from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from consultant_cli.errors import NotFoundError
from consultant_cli.infrastructure.store import ProjectStore, atomic_write_json, atomic_write_text


class ExportService:
    def __init__(self, store: ProjectStore):
        self.store = store

    def export(self, project_id: str, output_format: str) -> Path:
        project = self.store.load(project_id)
        directory = self.store.project_dir(project_id)
        export_dir = directory / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        instruction = directory / "03-instruction.md"
        if not instruction.exists():
            raise NotFoundError("Инструкция ещё не сформирована.")
        markdown = instruction.read_text(encoding="utf-8")
        if output_format in {"md", "markdown"}:
            path = export_dir / "result.md"
            atomic_write_text(path, markdown)
            return path
        if output_format == "json":
            path = export_dir / "result.json"
            artifacts = {}
            for item in sorted(directory.glob("*.md")):
                artifacts[item.name] = item.read_text(encoding="utf-8")
            evidence_path = directory / "evidence.ndjson"
            evidence = []
            if evidence_path.exists():
                evidence = [
                    json.loads(line)
                    for line in evidence_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            atomic_write_json(
                path,
                {
                    "project": project.to_dict(),
                    "artifacts": artifacts,
                    "evidence": evidence,
                    "events": self.store.read_events(project_id),
                },
            )
            return path
        if output_format == "html":
            path = export_dir / "result.html"
            atomic_write_text(path, markdown_to_html(markdown, project.title))
            return path
        raise ValueError(f"Неизвестный формат экспорта: {output_format}")


def markdown_to_html(markdown: str, title: str) -> str:
    _, _, body = markdown.partition("\n---\n") if markdown.startswith("---\n") else ("", "", markdown)
    escaped = html.escape(body)
    escaped = re.sub(r"^### (.+)$", r"<h3>\1</h3>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"^## (.+)$", r"<h2>\1</h2>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"^# (.+)$", r"<h1>\1</h1>", escaped, flags=re.MULTILINE)
    escaped = re.sub(r"^- (.+)$", r"<li>\1</li>", escaped, flags=re.MULTILINE)
    escaped = escaped.replace("\n", "<br>\n")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.55 system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#202124}}h1,h2,h3{{line-height:1.2}}code,pre{{background:#f4f5f7}}li{{margin:.25rem 0}}</style>
</head><body>{escaped}</body></html>\n"""

