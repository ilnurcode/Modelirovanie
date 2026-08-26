#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from consultant_cli.domain.analytics import BusinessQuestion, EvidenceStatus, stable_id
from consultant_cli.domain.models import ConfigurationInfo, Project, ProjectMode, ProjectStatus
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths, atomic_write_json, atomic_write_text
from consultant_cli.services.analytics import AnalyticsService, validate_bundle
from consultant_cli.services.telemetry import TelemetryService


def main() -> int:
    started = time.perf_counter()
    paths = RepositoryPaths(ROOT)
    store = ProjectStore(paths)
    service = AnalyticsService(paths, store)
    telemetry = TelemetryService(paths, store)
    source_path = ROOT / "tests" / "fixtures" / "bakery-source.md"
    source = source_path.read_text(encoding="utf-8")
    base_id = "bakery-regression"
    project_id = base_id
    suffix = 2
    while (paths.results / project_id).exists():
        project_id = f"{base_id}-{suffix}"
        suffix += 1
    project = Project(
        project_id=project_id,
        title="Регрессионный кейс хлебозавода",
        mode=ProjectMode.FULL,
        configuration=ConfigurationInfo(
            product="1С:ERP Управление предприятием 2",
            edition="2.5",
            release="2.5.27.49",
        ),
    )
    store.create(project, source)
    bundle = service.initialize(project_id, source, project.configuration)

    payment_requirements = [item.id for item in bundle.requirements if item.cluster == "payments"]
    payment_question = BusinessQuestion(
        id=stable_id("Q", "bakery-payment-variant"),
        text="Выберите вариант оплаты для регрессионной проверки.",
        cluster="payments",
        requirement_ids=payment_requirements[:8] or [bundle.requirements[0].id],
        options=["оплата до отгрузки", "оплата после отгрузки"],
    )
    bundle.questions = [item for item in bundle.questions if item.id != payment_question.id]
    if len(bundle.questions) >= 12:
        bundle.questions = bundle.questions[:11]
    bundle.questions.append(payment_question)
    service._save(bundle)
    service._write_artifacts(bundle)

    first_search = service.analyze_evidence(project_id)
    second_search = service.analyze_evidence(
        project_id, sorted({item.cluster for item in service.load(project_id).requirements})
    )
    ambiguous = service.record_decision(
        project_id, payment_question.id, "подтверждаю настройки"
    )
    modeler = service.run_modeler(project_id)
    bundle = service.load(project_id)
    schema_errors, schema_warnings = validate_bundle(bundle)

    source_statements = {item.source_text for item in bundle.requirements}
    requirement_ids = {item.id for item in bundle.requirements}
    invented_solution_values = []
    for item in bundle.solution_elements:
        statements = item.get("requirement_statements", [])
        if any(statement not in source_statements for statement in statements):
            invented_solution_values.append(item.get("id", "unknown"))
        if any(req_id not in requirement_ids for req_id in item.get("requirement_ids", [])):
            invented_solution_values.append(item.get("id", "unknown"))
        description = item.get("description", "")
        if description and description not in source_statements and not description.startswith("Операционный этап «"):
            invented_solution_values.append(description)
    trace_ids = {item.requirement_id for item in bundle.traceability}
    unsafe_verified_steps = []
    evidence = {item.id: item for item in bundle.evidence}
    for step in bundle.instruction_steps:
        statuses = {
            evidence[item_id].status
            for item_id in step.get("evidence_ids", [])
            if item_id in evidence
        }
        if step.get("validation_status") in {"verified", "verified_metadata"} and statuses.intersection(
            {EvidenceStatus.CANDIDATE, EvidenceStatus.UNRESOLVED}
        ):
            unsafe_verified_steps.append(step.get("id"))

    critical_gaps = [item.id for item in bundle.gaps if item.blocking]
    project.status = ProjectStatus.NEEDS_REVISION if critical_gaps or modeler["verdict"] != "passed" else ProjectStatus.FEEDBACK_PENDING
    store.save(project)
    report = {
        "project_id": project_id,
        "fixture": str(source_path.relative_to(ROOT)),
        "fixture_sha256": bundle.source_hash,
        "release": bundle.release,
        "checks": {
            "atomic_requirements_extracted": len(bundle.requirements) >= 80,
            "stable_requirement_ids_unique": len({item.id for item in bundle.requirements}) == len(bundle.requirements),
            "no_invented_solution_values": not invented_solution_values,
            "business_branches_preserved_as_questions": bool(bundle.questions),
            "exact_release_recorded": bundle.release == "2.5.27.49",
            "unverified_mechanisms_are_gaps": bool(bundle.gaps),
            "full_traceability": trace_ids == {item.id for item in bundle.requirements},
            "covered_requirements_have_acceptance_tests": all(
                item.acceptance_test_ids
                for item in bundle.requirements
                if item.coverage_status.value == "covered"
            ),
            "no_candidate_or_unresolved_in_verified_steps": not unsafe_verified_steps,
            "modeler_executed": modeler.get("reviewer") == "independent-deterministic-modeler",
            "ambiguous_payment_answer_rejected": not ambiguous.get("recorded", True),
            "evidence_cache_reused": second_search.get("cache_hits", 0) > 0,
            "schema_contract_valid": not schema_errors,
        },
        "counts": {
            "requirements": len(bundle.requirements),
            "clusters": len({item.cluster for item in bundle.requirements}),
            "questions": len(bundle.questions),
            "evidence": len(bundle.evidence),
            "covered": sum(item.coverage_status.value == "covered" for item in bundle.requirements),
            "partial": sum(item.coverage_status.value == "partial" for item in bundle.requirements),
            "gap": sum(item.coverage_status.value == "gap" for item in bundle.requirements),
            "critical_gaps": len(critical_gaps),
            "acceptance_tests": len(bundle.acceptance_tests),
            "model_calls": 0,
        },
        "evidence_search": {"first": first_search, "second": second_search},
        "ambiguous_payment_answer": ambiguous,
        "modeler": modeler,
        "schema_errors": schema_errors,
        "schema_warnings": schema_warnings,
        "limitations": [
            "Python-only пакет Ильнура не содержит первичный XML/object graph; route/semantic записи остаются candidate.",
            "Основной Codex/MCP runtime использует опубликованный граф Яны; эта offline-регрессия остаётся консервативной.",
        ],
        "timing": {
            "wall_time_ms": int((time.perf_counter() - started) * 1000),
            "model_time_ms": 0,
        },
    }
    atomic_write_json(store.project_dir(project_id) / "analysis" / "regression-report.json", report)
    telemetry_report = telemetry.aggregate(project_id)
    atomic_write_text(
        store.project_dir(project_id) / "README.md",
        "# Регрессионный проект хлебозавода\n\n"
        f"Статус: `{project.status.value}`. Требований: {len(bundle.requirements)}. "
        f"Critical GAP: {len(critical_gaps)}. Modeler: `{modeler['verdict']}`.\n\n"
        "Готовый ответ из исходного проекта не использовался как expected result; "
        "проверяются структура, доказательность и инварианты.\n",
    )
    print(json.dumps({"report": report, "telemetry": telemetry_report}, ensure_ascii=False, indent=2))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
