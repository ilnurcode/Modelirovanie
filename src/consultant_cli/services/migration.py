from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from consultant_cli.domain.analytics import CoverageStatus, Evidence, EvidenceStatus, Requirement, stable_id
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths
from consultant_cli.services.analytics import AnalyticsService


class MigrationService:
    """Data-only adapters. They do not import or execute source-project code."""

    def __init__(self, paths: RepositoryPaths, store: ProjectStore, analytics: AnalyticsService):
        self.paths = paths
        self.store = store
        self.analytics = analytics

    def bootstrap_kirill_project(self, project_id: str) -> dict[str, Any]:
        project = self.store.load(project_id)
        request = self.store.read_artifact(project_id, "00-request.md")
        bundle = self.analytics.initialize(project_id, request, project.configuration)
        return {
            "adapter": "kirill-project-v0.5",
            "project_id": project_id,
            "requirements": len(bundle.requirements),
            "provenance": "00-request.md + project.yaml; source code was not executed",
        }

    def import_yana_artifacts(
        self,
        project_id: str,
        requirement_map: Path,
        evidence_map: Path | None = None,
    ) -> dict[str, Any]:
        """Map JSON artifacts as untrusted data; unknown statuses stay candidate."""
        bundle = self.analytics.load(project_id)
        raw_requirements = json.loads(requirement_map.read_text(encoding="utf-8"))
        items = raw_requirements.get("requirements", raw_requirements.get("items", []))
        mapped: list[Requirement] = []
        for index, item in enumerate(items):
            source_text = str(item.get("source_text") or item.get("original_text") or item.get("statement") or "").strip()
            if not source_text:
                continue
            mapped.append(
                Requirement(
                    id=stable_id("REQ", source_text),
                    source_text=source_text,
                    cluster=str(item.get("cluster") or item.get("category") or "migrated"),
                    source={
                        "adapter": "yana-artifact-json-v1",
                        "source_file": str(requirement_map),
                        "source_index": index,
                        "legacy_id": item.get("requirement_id") or item.get("id"),
                    },
                    coverage_status=CoverageStatus.GAP,
                )
            )
        bundle.requirements = mapped
        bundle.evidence = []
        if evidence_map:
            raw_evidence = json.loads(evidence_map.read_text(encoding="utf-8"))
            claims = raw_evidence.get("evidence", raw_evidence.get("claims", []))
            for item in claims:
                legacy_status = str(item.get("status", "candidate"))
                status = (
                    EvidenceStatus.CANDIDATE
                    if legacy_status not in {"verified_source", "verified_metadata", "user_decision"}
                    else EvidenceStatus(legacy_status)
                )
                ref = str(item.get("source_ref") or item.get("id") or item.get("claim") or "")
                bundle.evidence.append(
                    Evidence(
                        id=stable_id("EVD", "yana-data", ref),
                        source_type="migrated_yana_data",
                        product=bundle.product,
                        release=bundle.release,
                        source_ref=ref,
                        excerpt=str(item.get("excerpt") or item.get("claim") or "")[:900],
                        status=status,
                    )
                )
        bundle.revision += 1
        bundle.requirements_approved_revision = None
        bundle.design_approved_revision = None
        bundle.final_approved_revision = None
        bundle.dirty_clusters = sorted({item.cluster for item in mapped})
        self.analytics._save(bundle)
        self.analytics._write_artifacts(bundle)
        return {
            "adapter": "yana-artifact-json-v1",
            "project_id": project_id,
            "requirements": len(mapped),
            "evidence_candidates": len(bundle.evidence),
            "provenance": {
                "requirement_map": str(requirement_map),
                "evidence_map": str(evidence_map) if evidence_map else "",
                "policy": "data-only; source code not copied or executed",
            },
        }
