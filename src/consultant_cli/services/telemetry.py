from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from consultant_cli import __version__
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths, atomic_write_json


class TelemetryService:
    def __init__(self, paths: RepositoryPaths, store: ProjectStore):
        self.paths = paths
        self.store = store

    def graph_identity(self) -> tuple[str, str]:
        graphs = self.paths.modeler_graphs()
        manifest = graphs / "graph_manifest.json"
        index = graphs / "search-index.ndjson.gz"
        version = "missing"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                version = f"{data.get('configuration', '')} {data.get('release', '')}".strip()
            except json.JSONDecodeError:
                version = "invalid-manifest"
        digest = hashlib.sha256()
        if index.exists():
            with index.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        return version, digest.hexdigest() if index.exists() else "missing"

    def skill_version(self, skill: str) -> str:
        skill_name = skill.split(":", 1)[0]
        candidates = [
            self.paths.root / "skills" / skill_name / "SKILL.md",
            self.paths.root / "1c_modeler_upgrade" / "SKILL.md",
        ]
        path = next((item for item in candidates if item.exists()), None)
        if not path:
            return "missing"
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    def record(
        self,
        project_id: str,
        *,
        provider: str,
        model: str,
        reasoning_effort: str,
        skill: str,
        attempt: int,
        result: str,
        error: str = "",
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        duration_ms: int = 0,
        wall_time_ms: int | None = None,
    ) -> dict[str, Any]:
        graph_version, graph_hash = self.graph_identity()
        record = {
            "call_id": str(uuid.uuid4()),
            "timestamp_ms": int(time.time() * 1000),
            "provider": provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "skill": skill,
            "skill_version": self.skill_version(skill),
            "application_version": __version__,
            "graph_version": graph_version,
            "graph_hash": graph_hash,
            "input_tokens": int(input_tokens or 0),
            "cached_input_tokens": int(cached_input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "reasoning_tokens": int(reasoning_tokens or 0),
            "duration_ms": int(duration_ms or 0),
            "wall_time_ms": int(wall_time_ms if wall_time_ms is not None else duration_ms or 0),
            "attempt": int(attempt),
            "result": result,
            "error": error,
        }
        path = self.store.project_dir(project_id) / "telemetry" / "model-calls.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.aggregate(project_id)
        return record

    def records(self, project_id: str) -> list[dict[str, Any]]:
        path = self.store.project_dir(project_id) / "telemetry" / "model-calls.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def aggregate(self, project_id: str) -> dict[str, Any]:
        records = self.records(project_id)
        totals = {
            "completed_model_calls": sum(item.get("result") == "completed" for item in records),
            "failed_model_calls": sum(item.get("result") == "failed" for item in records),
            "input_tokens": sum(int(item.get("input_tokens", 0)) for item in records),
            "cached_input_tokens": sum(int(item.get("cached_input_tokens", 0)) for item in records),
            "output_tokens": sum(int(item.get("output_tokens", 0)) for item in records),
            "reasoning_tokens": sum(int(item.get("reasoning_tokens", 0)) for item in records),
            "model_time_ms": sum(int(item.get("duration_ms", 0)) for item in records),
            "wall_time_ms": sum(int(item.get("wall_time_ms", 0)) for item in records),
        }
        over_budget = totals["completed_model_calls"] > 30
        report = {
            "project_id": project_id,
            "budget": {"target_completed_model_calls": 30, "exceeded": over_budget},
            "totals": totals,
            "explanation": (
                "Целевой бюджет превышен; см. журнал вызовов и повторные попытки."
                if over_budget
                else "Целевой бюджет модельных вызовов соблюдён."
            ),
        }
        atomic_write_json(self.store.project_dir(project_id) / "telemetry" / "report.json", report)
        return report
