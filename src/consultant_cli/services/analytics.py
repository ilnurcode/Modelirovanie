from __future__ import annotations

import copy
import gzip
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from consultant_cli.domain.analytics import (
    AcceptanceTest,
    AnalysisBundle,
    BusinessQuestion,
    CoverageStatus,
    Decision,
    Evidence,
    EvidenceStatus,
    Gap,
    Requirement,
    StepValidationStatus,
    TraceLink,
    stable_id,
)
from consultant_cli.domain.models import ConfigurationInfo
from consultant_cli.errors import NotFoundError, WorkflowBlockedError
from consultant_cli.infrastructure.store import (
    ProjectStore,
    RepositoryPaths,
    atomic_write_json,
)


WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
NUMBERED = re.compile(r"^\s*(?:[-*]|\d+[.)]|\d+\.[a-zа-я][.)])\s+(.*)$", re.IGNORECASE)
UNCERTAINTY = re.compile(
    r"требу(?:ет|ется)\s+уточнен|не\s+определ|неизвест|\bили\b|\bлибо\b|\bвариант",
    re.IGNORECASE,
)

CLUSTER_RULES = (
    ("payments", ("оплат", "задолж", "казнач", "деньг", "банк", "касс")),
    ("sales", ("клиент", "заказ", "продаж", "отгруз", "возврат")),
    ("procurement", ("поставщик", "закуп", "поставк", "сырь")),
    ("production", ("производ", "выпуск", "рецепт", "спецификац", "цех")),
    ("warehouse", ("склад", "остат", "размещ", "парт", "сер", "хранен")),
    ("quality", ("качеств", "брак", "контрол", "приемк", "приёмк")),
    ("master-data", ("номенклат", "справочник", "нси", "единиц", "упаков")),
)


def _tokens(value: str) -> set[str]:
    stop = {"для", "или", "при", "как", "это", "что", "если", "после", "перед", "через"}
    return {
        token.casefold().replace("ё", "е")
        for token in WORD.findall(value)
        if len(token) >= 4 and token.casefold() not in stop
    }


def _normalized(value: str) -> str:
    return " ".join(WORD.findall(value.casefold().replace("ё", "е")))


def _cluster(value: str, heading: str = "") -> str:
    folded = f"{heading} {value}".casefold().replace("ё", "е")
    for name, needles in CLUSTER_RULES:
        if any(needle in folded for needle in needles):
            return name
    return stable_id("cluster", heading or value, size=8).casefold()


@dataclass(slots=True)
class EvidenceBatchResult:
    by_cluster: dict[str, list[Evidence]]
    cache_hits: int
    cache_misses: int
    graph_hash: str


class BatchEvidenceSearcher:
    """One-pass graph search with content-addressed, release-aware cache."""

    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        graphs = paths.modeler_graphs()
        self.index = graphs / "search-index.ndjson.gz"
        self.manifest_path = graphs / "graph_manifest.json"
        self.cache_dir = paths.data_root / ".cache" / "evidence"

    def search(
        self,
        configuration: ConfigurationInfo,
        source_hash: str,
        intents: dict[str, str],
        limit: int = 8,
    ) -> EvidenceBatchResult:
        graph_hash = self._hash(self.index) if self.index.exists() else "missing"
        manifest = self._manifest()
        cached: dict[str, list[Evidence]] = {}
        misses: dict[str, str] = {}
        cache_hits = 0
        for cluster, intent in intents.items():
            key = stable_id(
                "cache",
                configuration.product,
                configuration.release,
                source_hash,
                intent,
                graph_hash,
                size=32,
            )
            path = self.cache_dir / f"{key}.json"
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    cached[cluster] = [Evidence.from_dict(item) for item in payload["evidence"]]
                    cache_hits += 1
                    continue
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    pass
            misses[cluster] = intent

        found = self._scan(configuration, manifest, misses, limit) if misses else {}
        for cluster, intent in misses.items():
            evidence = found.get(cluster, [])
            cached[cluster] = evidence
            key = stable_id(
                "cache",
                configuration.product,
                configuration.release,
                source_hash,
                intent,
                graph_hash,
                size=32,
            )
            atomic_write_json(
                self.cache_dir / f"{key}.json",
                {
                    "product": configuration.product,
                    "release": configuration.release,
                    "source_hash": source_hash,
                    "intent": intent,
                    "graph_hash": graph_hash,
                    "evidence": [item.to_dict() for item in evidence],
                },
            )
        return EvidenceBatchResult(cached, cache_hits, len(misses), graph_hash)

    def _scan(
        self,
        configuration: ConfigurationInfo,
        manifest: dict[str, Any],
        intents: dict[str, str],
        limit: int,
    ) -> dict[str, list[Evidence]]:
        if not self.index.exists():
            return {cluster: [] for cluster in intents}
        intent_tokens = {cluster: _tokens(text) for cluster, text in intents.items()}
        scored: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        with gzip.open(self.index, "rt", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                haystack = " ".join(
                    [
                        str(record.get("label", "")),
                        str(record.get("type", "")),
                        json.dumps(record.get("properties", {}), ensure_ascii=False),
                    ]
                )
                words = _tokens(haystack)
                for cluster, wanted in intent_tokens.items():
                    score = len(words & wanted)
                    if score:
                        scored[cluster].append((score, record))

        result: dict[str, list[Evidence]] = {}
        for cluster in intents:
            records = sorted(
                scored.get(cluster, []),
                key=lambda item: (-item[0], str(item[1].get("graph", "")), str(item[1].get("id", ""))),
            )
            seen: set[tuple[str, str]] = set()
            evidence: list[Evidence] = []
            for _, record in records:
                key = (str(record.get("graph", "")), str(record.get("id", "")))
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(self._to_evidence(record, configuration, manifest))
                if len(evidence) >= limit:
                    break
            result[cluster] = evidence
        return result

    def _to_evidence(
        self,
        record: dict[str, Any],
        configuration: ConfigurationInfo,
        manifest: dict[str, Any],
    ) -> Evidence:
        graph = str(record.get("graph", "unknown"))
        record_product = str(record.get("configuration", ""))
        record_release = str(record.get("release", ""))
        exact = (
            _normalized(configuration.product) == _normalized(record_product)
            and bool(configuration.release)
            and configuration.release == record_release
        )
        graph_status = self._graph_status(manifest, graph)
        primary_ref = self._primary_ref(record)
        source_available = bool(primary_ref and Path(primary_ref).is_file())
        if exact and graph in {"object", "route", "semantic"} and graph_status == "ГОТОВ" and source_available:
            status = EvidenceStatus.VERIFIED_METADATA
        elif exact and graph == "source" and graph_status == "ГОТОВ" and source_available:
            status = EvidenceStatus.VERIFIED_SOURCE
        else:
            status = EvidenceStatus.CANDIDATE
        properties = record.get("properties", {}) or {}
        record_id = str(record.get("id", ""))
        record_type = str(record.get("type", ""))
        excerpt = str(properties.get("search_text", record.get("label", "")))[:900]
        is_route = graph == "route" or "route" in record_type.casefold()
        is_edge = record_type.casefold() in {"edge", "relation"} or bool(properties.get("relationship"))
        return Evidence(
            id=stable_id("EVD", graph, record_id, configuration.product, configuration.release),
            source_type=graph,
            product=configuration.product,
            release=configuration.release,
            source_ref=primary_ref or record_id,
            object_ref="" if is_route or is_edge else record_id,
            route_ref=record_id if is_route else "",
            edge_ref=record_id if is_edge else "",
            excerpt=excerpt,
            status=status,
        )

    @staticmethod
    def _primary_ref(record: dict[str, Any]) -> str:
        properties = record.get("properties", {}) or {}
        return str(
            properties.get("source_xml")
            or properties.get("source_ref")
            or properties.get("source_path")
            or properties.get("path")
            or ""
        )

    @staticmethod
    def _graph_status(manifest: dict[str, Any], graph: str) -> str:
        for item in manifest.get("graphs", []):
            filename = str(item.get("file", "")).casefold()
            if graph.casefold() in filename:
                return str(item.get("status", ""))
        return ""

    def _manifest(self) -> dict[str, Any]:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


class AnalyticsModeler:
    """Independent, deterministic reviewer; it never mutates project status."""

    def review(self, bundle: AnalysisBundle) -> dict[str, Any]:
        errors, warnings = validate_bundle(bundle)
        evidence = {item.id: item for item in bundle.evidence}
        for step in bundle.instruction_steps:
            statuses = {
                evidence[item_id].status
                for item_id in step.get("evidence_ids", [])
                if item_id in evidence
            }
            if step.get("object_ref") and EvidenceStatus.VERIFIED_METADATA not in statuses:
                errors.append(f"{step.get('id')}: unknown ERP object")
            if step.get("route_ref") and EvidenceStatus.VERIFIED_METADATA not in statuses:
                errors.append(f"{step.get('id')}: unresolved user route")
            if step.get("edge_ref") and not statuses.intersection(
                {EvidenceStatus.VERIFIED_METADATA, EvidenceStatus.VERIFIED_SOURCE}
            ):
                errors.append(f"{step.get('id')}: unverified inter-object relation")
            if step.get("validation_status") in {"verified", "verified_metadata"} and statuses.intersection(
                {EvidenceStatus.CANDIDATE, EvidenceStatus.UNRESOLVED}
            ):
                errors.append(f"{step.get('id')}: candidate claim presented as verified")
        unique_errors = sorted(set(errors))
        return {
            "reviewer": "independent-deterministic-modeler",
            "revision": bundle.revision,
            "verdict": "passed" if not unique_errors else "needs_revision",
            "errors": unique_errors,
            "warnings": sorted(set(warnings)),
            "counts": {
                "requirements": len(bundle.requirements),
                "evidence": len(bundle.evidence),
                "gaps": len(bundle.gaps),
                "trace_links": len(bundle.traceability),
                "acceptance_tests": len(bundle.acceptance_tests),
            },
        }


def validate_bundle(bundle: AnalysisBundle) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    requirements = {item.id: item for item in bundle.requirements}
    evidence = {item.id for item in bundle.evidence}
    decisions = {item.id for item in bundle.decisions}
    tests = {item.id: item for item in bundle.acceptance_tests}
    solution_ids = {str(item.get("id")) for item in bundle.solution_elements}
    step_ids = {str(item.get("id")) for item in bundle.instruction_steps}
    traces = {item.requirement_id: item for item in bundle.traceability}

    for requirement in bundle.requirements:
        has_gap = any(requirement.id in gap.requirement_ids for gap in bundle.gaps)
        if not requirement.solution_element_ids and not has_gap:
            errors.append(f"{requirement.id}: requirement has neither solution nor GAP")
        if requirement.coverage_status is CoverageStatus.COVERED and not requirement.acceptance_test_ids:
            errors.append(f"{requirement.id}: covered requirement has no acceptance test")
        if any(item not in evidence for item in requirement.evidence_ids):
            errors.append(f"{requirement.id}: unknown evidence id")
        if any(item not in decisions for item in requirement.decision_ids):
            errors.append(f"{requirement.id}: unknown decision id")
        if any(item not in solution_ids for item in requirement.solution_element_ids):
            errors.append(f"{requirement.id}: unknown solution element id")
        if any(item not in tests for item in requirement.acceptance_test_ids):
            errors.append(f"{requirement.id}: unknown acceptance test id")
        trace = traces.get(requirement.id)
        if not trace:
            errors.append(f"{requirement.id}: missing traceability row")
            continue
        if set(trace.decision_ids) != set(requirement.decision_ids):
            errors.append(f"{requirement.id}: decision ids differ in traceability")
        if set(trace.evidence_ids) != set(requirement.evidence_ids):
            errors.append(f"{requirement.id}: evidence ids differ in traceability")
        if set(trace.solution_element_ids) != set(requirement.solution_element_ids):
            errors.append(f"{requirement.id}: solution ids differ in traceability")
        if set(trace.acceptance_test_ids) != set(requirement.acceptance_test_ids):
            errors.append(f"{requirement.id}: test ids differ in traceability")
        if any(item not in step_ids for item in trace.instruction_step_ids):
            errors.append(f"{requirement.id}: unknown instruction step id")

    for test in bundle.acceptance_tests:
        if not test.requirement_ids or any(item not in requirements for item in test.requirement_ids):
            errors.append(f"{test.id}: invalid requirement ids")
    for gap in bundle.gaps:
        if any(item not in requirements for item in gap.requirement_ids):
            errors.append(f"{gap.id}: invalid requirement ids")
        if gap.blocking:
            warnings.append(f"{gap.id}: critical gap blocks successful")
    if len(bundle.questions) > 12:
        errors.append("main question batch exceeds 12 questions")
    return sorted(set(errors)), sorted(set(warnings))


class AnalyticsService:
    def __init__(self, paths: RepositoryPaths, store: ProjectStore):
        self.paths = paths
        self.store = store
        self.searcher = BatchEvidenceSearcher(paths)
        self.modeler = AnalyticsModeler()

    def initialize(
        self,
        project_id: str,
        source_text: str,
        configuration: ConfigurationInfo,
        revision: int = 1,
    ) -> AnalysisBundle:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        requirements = self.extract_requirements(source_text, source_hash)
        bundle = AnalysisBundle(
            project_id=project_id,
            product=configuration.product,
            release=configuration.release,
            source_hash=source_hash,
            revision=max(1, int(revision)),
            requirements=requirements,
            dirty_clusters=sorted({item.cluster for item in requirements}),
        )
        bundle.questions = self.build_questions(requirements)
        self._save(bundle)
        self._write_artifacts(bundle)
        return bundle

    def ensure(self, project_id: str) -> AnalysisBundle:
        """Load analytics or bootstrap a legacy project without an LLM call."""
        try:
            return self.load(project_id)
        except NotFoundError:
            project = self.store.load(project_id)
            source_text = self.store.read_artifact(project_id, "00-request.md")
            bundle = self.initialize(
                project_id,
                source_text,
                project.configuration,
                revision=project.revision,
            )
            self.store.append_event(
                project_id,
                "legacy_analysis_initialized",
                {
                    "revision": bundle.revision,
                    "source": "00-request.md",
                    "llm_calls": 0,
                },
            )
            return bundle

    def reconfigure(self, project_id: str, configuration: ConfigurationInfo) -> AnalysisBundle:
        bundle = self.ensure(project_id)
        if bundle.product == configuration.product and bundle.release == configuration.release:
            return bundle
        self._snapshot(bundle)
        bundle.revision += 1
        bundle.product = configuration.product
        bundle.release = configuration.release
        bundle.evidence = []
        bundle.gaps = []
        bundle.solution_elements = []
        bundle.instruction_steps = []
        bundle.traceability = []
        bundle.acceptance_tests = []
        bundle.requirements_approved_revision = None
        bundle.design_approved_revision = None
        bundle.final_approved_revision = None
        bundle.schema_valid = False
        bundle.modeler_passed = False
        bundle.dirty_clusters = sorted({item.cluster for item in bundle.requirements})
        for requirement in bundle.requirements:
            requirement.evidence_ids = []
            requirement.solution_element_ids = []
            requirement.acceptance_test_ids = []
            requirement.coverage_status = CoverageStatus.GAP
        self._save(bundle)
        self._write_artifacts(bundle)
        return bundle

    def revoke_approvals(
        self, project_id: str, from_stage: str = "requirements"
    ) -> AnalysisBundle:
        """Revoke the changed stage and every downstream approval.

        Instruction-only feedback must not silently revoke already approved
        requirements and design.  The default preserves the previous strict
        behavior for changes whose scope is not specified.
        """
        bundle = self.ensure(project_id)
        if from_stage not in {"requirements", "design", "instruction"}:
            raise WorkflowBlockedError(f"Неизвестный этап отзыва approval: {from_stage}")
        if from_stage == "requirements":
            bundle.requirements_approved_revision = None
        if from_stage in {"requirements", "design"}:
            bundle.design_approved_revision = None
        bundle.final_approved_revision = None
        bundle.modeler_passed = False
        self._save(bundle)
        self._write_artifacts(bundle)
        return bundle

    def extract_requirements(self, text: str, source_hash: str) -> list[Requirement]:
        heading = "Общее"
        raw_items: list[tuple[int, str, str]] = []
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line == "---":
                continue
            if line.startswith(">"):
                line = line.lstrip("> ").strip()
                if not line:
                    continue
            if line.startswith("#"):
                heading = line.lstrip("# ").strip() or heading
                continue
            if re.fullmatch(
                r"\*{0,2}(?:Входные данные|Действия|Результат|Контрольные точки|Особенности):\*{0,2}",
                line,
                re.IGNORECASE,
            ):
                continue
            match = NUMBERED.match(line)
            candidates = [match.group(1).strip()] if match else re.split(r"(?<=[.!?])\s+", line)
            for candidate in candidates:
                candidate = candidate.strip(" -*\t").replace("**", "")
                if len(candidate) < 12 or candidate.startswith(("artifact:", "project_id:", "created_at:")):
                    continue
                raw_items.append((line_no, heading, candidate))
        requirements: list[Requirement] = []
        seen: set[str] = set()
        for line_no, item_heading, source_text in raw_items:
            requirement_id = stable_id("REQ", source_text)
            if requirement_id in seen:
                continue
            seen.add(requirement_id)
            requirements.append(
                Requirement(
                    id=requirement_id,
                    source_text=source_text,
                    cluster=_cluster(source_text, item_heading),
                    source={"source_hash": source_hash, "line": line_no, "heading": item_heading},
                )
            )
        return requirements

    def build_questions(self, requirements: Iterable[Requirement]) -> list[BusinessQuestion]:
        by_cluster: dict[str, list[Requirement]] = defaultdict(list)
        for requirement in requirements:
            if UNCERTAINTY.search(requirement.source_text):
                by_cluster[requirement.cluster].append(requirement)
        questions: list[BusinessQuestion] = []
        for cluster in sorted(by_cluster):
            items = by_cluster[cluster]
            text = items[0].source_text
            options = self._options(text)
            questions.append(
                BusinessQuestion(
                    id=stable_id("Q", cluster, "|".join(item.id for item in items)),
                    text=f"Уточните бизнес-вариант для кластера «{cluster}»: {text}",
                    cluster=cluster,
                    requirement_ids=[item.id for item in items],
                    options=options,
                )
            )
            if len(questions) == 12:
                break
        return questions

    def synchronize_presented_questions(
        self, project_id: str, questions: Iterable[dict[str, Any]]
    ) -> AnalysisBundle:
        """Make the translator package shown to the user canonical for approvals."""
        bundle = self.ensure(project_id)
        requirements = {item.id: item for item in bundle.requirements}
        synchronized: list[BusinessQuestion] = []
        for raw in questions:
            question_id = str(raw.get("id") or "").strip()
            text = str(raw.get("text") or "").strip()
            if not question_id or not text:
                continue
            impact = str(raw.get("impact") or "")
            explicit_ids = [
                item
                for item in re.findall(r"REQ-[0-9a-f]+", impact, re.IGNORECASE)
                if item in requirements
            ]
            if not explicit_ids:
                wanted = _tokens(text + " " + impact)
                scored = sorted(
                    (
                        (len(wanted & _tokens(item.source_text)), item.id)
                        for item in requirements.values()
                    ),
                    reverse=True,
                )
                explicit_ids = [item_id for score, item_id in scored[:4] if score > 0]
            cluster = (
                requirements[explicit_ids[0]].cluster
                if explicit_ids
                else "business-decisions"
            )
            synchronized.append(
                BusinessQuestion(
                    id=question_id,
                    text=text,
                    cluster=cluster,
                    requirement_ids=list(dict.fromkeys(explicit_ids)),
                    options=[str(item) for item in raw.get("options", []) if str(item).strip()],
                    required=bool(raw.get("required", True)),
                )
            )
        if not synchronized:
            raise WorkflowBlockedError("Показанный пакет вопросов пуст или повреждён.")
        visible_ids = {item.id for item in synchronized}
        bundle.questions = synchronized
        bundle.decisions = [
            item for item in bundle.decisions if item.question_id in visible_ids
        ]
        self._save(bundle)
        self._write_artifacts(bundle)
        return bundle

    @staticmethod
    def _options(text: str) -> list[str]:
        clean = re.sub(r"\([^)]*требу(?:ет|ется)\s+уточнен[^)]*\)", "", text, flags=re.I)
        parts = [part.strip(" .,:;-") for part in re.split(r"\s+(?:или|либо)\s+", clean, flags=re.I)]
        return parts if 2 <= len(parts) <= 4 and all(parts) else []

    def analyze_evidence(self, project_id: str, clusters: Iterable[str] | None = None) -> dict[str, Any]:
        bundle = self.ensure(project_id)
        selected = set(clusters or bundle.dirty_clusters or {item.cluster for item in bundle.requirements})
        requirements_by_cluster: dict[str, list[Requirement]] = defaultdict(list)
        for requirement in bundle.requirements:
            if requirement.cluster in selected:
                requirements_by_cluster[requirement.cluster].append(requirement)
        intents = {
            cluster: " ".join(item.source_text for item in items)[:5000]
            for cluster, items in requirements_by_cluster.items()
        }
        result = self.searcher.search(
            ConfigurationInfo(bundle.product, release=bundle.release),
            bundle.source_hash,
            intents,
        )
        unaffected = [item for item in bundle.evidence if not item.id.startswith("EVD-")]
        affected_ids = {item.id for reqs in requirements_by_cluster.values() for item in reqs}
        retained_ids = {
            item_id
            for requirement in bundle.requirements
            if requirement.id not in affected_ids
            for item_id in requirement.evidence_ids
        }
        retained = [item for item in bundle.evidence if item.id in retained_ids]
        new_evidence: dict[str, Evidence] = {item.id: item for item in (*unaffected, *retained)}
        for cluster, evidence in result.by_cluster.items():
            ids = []
            for item in evidence:
                new_evidence[item.id] = item
                ids.append(item.id)
            for requirement in requirements_by_cluster.get(cluster, []):
                requirement.evidence_ids = ids
        bundle.evidence = list(new_evidence.values())
        self._rebuild_derived(bundle, selected)
        bundle.dirty_clusters = sorted(set(bundle.dirty_clusters) - selected)
        self._save(bundle)
        self._write_artifacts(bundle)
        return {
            "project_id": project_id,
            "rebuilt_clusters": sorted(selected),
            "cache_hits": result.cache_hits,
            "cache_misses": result.cache_misses,
            "graph_hash": result.graph_hash,
            "evidence_count": len(bundle.evidence),
        }

    def record_decision(self, project_id: str, question_id: str, exact_answer: str) -> dict[str, Any]:
        current = self.ensure(project_id)
        question = next((item for item in current.questions if item.id == question_id), None)
        if not question:
            raise NotFoundError(f"Вопрос не найден: {question_id}")
        normalized = self._resolve_answer(exact_answer, question.options)
        if normalized is None:
            follow_up = not question.follow_up_used
            if follow_up:
                question.follow_up_used = True
                self._save(current)
            return {
                "recorded": False,
                "needs_clarification": follow_up,
                "question_id": question_id,
                "options": question.options,
                "reason": "Ответ не выбирает ровно один вариант.",
            }
        bundle = copy.deepcopy(current)
        self._snapshot(current)
        bundle.revision += 1
        decision_id = stable_id("DEC", question_id)
        decision = Decision(
            id=decision_id,
            question_id=question_id,
            exact_user_answer=exact_answer.strip(),
            normalized_value=normalized,
            revision=bundle.revision,
            affected_requirement_ids=list(question.requirement_ids),
        )
        bundle.decisions = [item for item in bundle.decisions if item.id != decision_id] + [decision]
        affected_clusters: set[str] = set()
        for requirement in bundle.requirements:
            if requirement.id in question.requirement_ids:
                if decision_id not in requirement.decision_ids:
                    requirement.decision_ids.append(decision_id)
                affected_clusters.add(requirement.cluster)
        bundle.requirements_approved_revision = None
        bundle.design_approved_revision = None
        bundle.final_approved_revision = None
        bundle.modeler_passed = False
        bundle.dirty_clusters = sorted(set(bundle.dirty_clusters) | affected_clusters)
        self._rebuild_derived(bundle, affected_clusters)
        self._save(bundle)
        self._write_artifacts(bundle)
        return {
            "recorded": True,
            "decision_id": decision_id,
            "revision": bundle.revision,
            "affected_requirement_ids": question.requirement_ids,
            "rebuilt_clusters": sorted(affected_clusters),
            "approvals_revoked": True,
        }

    @staticmethod
    def _resolve_answer(answer: str, options: list[str]) -> str | None:
        value = _normalized(answer)
        if not value:
            return None
        if not options:
            return value
        normalized_options = [(_normalized(option), option) for option in options]
        matches = [
            original
            for normalized, original in normalized_options
            if normalized in value or value == normalized
        ]
        if len(matches) == 1:
            return _normalized(matches[0])
        generic = {"да", "нет", "подтверждаю", "согласен", "ок", "утвердить"}
        if any(value == item or value.startswith(item + " ") for item in generic):
            prefix_matches = [
                original
                for normalized, original in normalized_options
                if normalized == value or normalized.startswith(value + " ")
            ]
            return _normalized(prefix_matches[0]) if len(prefix_matches) == 1 else None
        # Options are recommended mutually exclusive examples, not a ban on an
        # exact free-form business decision supplied by the user.
        return value

    def approve(
        self,
        project_id: str,
        stage: str,
        *,
        verified_instruction_artifact: bool = False,
    ) -> AnalysisBundle:
        bundle = self.ensure(project_id)
        if stage == "requirements":
            unanswered = [
                item.id
                for item in bundle.questions
                if item.required
                and not any(d.question_id == item.id for d in bundle.decisions)
            ]
            if unanswered:
                raise WorkflowBlockedError("Не закрыты бизнес-вопросы: " + ", ".join(unanswered))
            bundle.requirements_approved_revision = bundle.revision
        elif stage == "design":
            if bundle.requirements_approved_revision != bundle.revision:
                raise WorkflowBlockedError("Текущая ревизия требований не утверждена.")
            bundle.design_approved_revision = bundle.revision
        elif stage == "instruction":
            if bundle.design_approved_revision != bundle.revision:
                raise WorkflowBlockedError("Текущая ревизия проекта решения не утверждена.")
            errors, _ = validate_bundle(bundle)
            report = (
                {
                    "reviewer": "verified-instruction-artifact-gate",
                    "revision": bundle.revision,
                    "verdict": "passed" if not errors else "needs_revision",
                    "errors": sorted(set(errors)),
                    "warnings": [
                        "Предварительные аналитические GAP сохранены для трассировки; "
                        "финальный gate выполнен по contract_passed и точному Modeler-report инструкции."
                    ],
                    "counts": {
                        "requirements": len(bundle.requirements),
                        "evidence": len(bundle.evidence),
                        "gaps": len(bundle.gaps),
                        "trace_links": len(bundle.traceability),
                        "acceptance_tests": len(bundle.acceptance_tests),
                    },
                }
                if verified_instruction_artifact
                else self.modeler.review(bundle)
            )
            bundle.schema_valid = not errors
            bundle.modeler_passed = report["verdict"] == "passed"
            if errors or not bundle.modeler_passed:
                self._save(bundle)
                self._write_modeler(project_id, report)
                raise WorkflowBlockedError("Технический approval заблокирован: " + "; ".join(report["errors"][:5]))
            blockers = (
                []
                if verified_instruction_artifact
                else [gap.id for gap in bundle.gaps if gap.blocking]
            )
            if blockers:
                raise WorkflowBlockedError("Critical GAP блокирует successful: " + ", ".join(blockers))
            bundle.final_approved_revision = bundle.revision
            self._write_modeler(project_id, report)
        else:
            raise WorkflowBlockedError(f"Неизвестный этап аналитического approval: {stage}")
        self._save(bundle)
        self._write_artifacts(bundle)
        return bundle

    def run_modeler(self, project_id: str) -> dict[str, Any]:
        bundle = self.ensure(project_id)
        report = self.modeler.review(bundle)
        bundle.schema_valid = not validate_bundle(bundle)[0]
        bundle.modeler_passed = report["verdict"] == "passed"
        self._save(bundle)
        self._write_modeler(project_id, report)
        return report

    def load(self, project_id: str) -> AnalysisBundle:
        path = self._analysis_dir(project_id) / "analysis.json"
        if not path.exists():
            raise NotFoundError(f"Аналитическая модель не найдена: {project_id}")
        return AnalysisBundle.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def author_context(self, project_id: str, max_chars: int = 14_000) -> str:
        bundle = self.load(project_id)
        clusters: dict[str, list[dict[str, Any]]] = {}
        for item in bundle.requirements:
            clusters.setdefault(item.cluster, []).append(
                {
                    "id": item.id,
                    "text": item.source_text[:100],
                    "coverage": item.coverage_status.value,
                    "decisions": item.decision_ids,
                    "evidence": item.evidence_ids,
                }
            )
        payload = {
            "revision": bundle.revision,
            "product": bundle.product,
            "release": bundle.release,
            "requirements_by_cluster": clusters,
            "operational_steps": bundle.instruction_steps,
            "decisions": [item.to_dict() for item in bundle.decisions],
            "evidence": [
                {
                    "id": item.id,
                    "status": item.status.value,
                    "source_ref": item.source_ref,
                    "object_ref": item.object_ref,
                    "route_ref": item.route_ref,
                    "edge_ref": item.edge_ref,
                    "excerpt": item.excerpt[:240],
                }
                for item in bundle.evidence[:30]
            ],
            "gaps": [
                {
                    "id": item.id,
                    "requirement_ids": item.requirement_ids,
                    "criticality": item.criticality,
                    "status": item.status,
                }
                for item in bundle.gaps[:30]
            ],
            "traceability_complete": len(bundle.traceability) == len(bundle.requirements),
            "omitted": {
                "requirements": 0,
                "evidence": max(0, len(bundle.evidence) - 30),
                "gaps": max(0, len(bundle.gaps) - 30),
            },
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # Preserve every requirement ID. Reduce supporting excerpts before dropping facts.
        if len(text) > max_chars:
            for item in payload["evidence"]:
                item.pop("excerpt", None)
            payload["evidence"] = payload["evidence"][:15]
            payload["gaps"] = payload["gaps"][:15]
            payload["omitted"]["evidence"] = len(bundle.evidence) - len(payload["evidence"])
            payload["omitted"]["gaps"] = len(bundle.gaps) - len(payload["gaps"])
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(text) > max_chars:
            for requirements in payload["requirements_by_cluster"].values():
                for item in requirements:
                    item["text"] = item["text"][:45]
                    item.pop("evidence", None)
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return text

    def _rebuild_derived(self, bundle: AnalysisBundle, clusters: Iterable[str]) -> None:
        selected = set(clusters)
        evidence = {item.id: item for item in bundle.evidence}
        bundle.solution_elements = [
            item for item in bundle.solution_elements if item.get("cluster") not in selected
        ]
        bundle.instruction_steps = [
            item for item in bundle.instruction_steps if item.get("cluster") not in selected
        ]
        selected_requirement_ids = {item.id for item in bundle.requirements if item.cluster in selected}
        bundle.gaps = [
            item for item in bundle.gaps if not set(item.requirement_ids).intersection(selected_requirement_ids)
        ]
        bundle.acceptance_tests = [
            item for item in bundle.acceptance_tests if not set(item.requirement_ids).intersection(selected_requirement_ids)
        ]
        relevant_by_requirement: dict[str, list[Evidence]] = {}
        operational_groups: dict[tuple[str, str, str], list[Requirement]] = {}
        for requirement in bundle.requirements:
            if requirement.cluster not in selected:
                continue
            relevant = [evidence[item] for item in requirement.evidence_ids if item in evidence]
            relevant_by_requirement[requirement.id] = relevant
            statuses = {item.status for item in relevant}
            if statuses.intersection({EvidenceStatus.VERIFIED_SOURCE, EvidenceStatus.VERIFIED_METADATA}):
                requirement.coverage_status = CoverageStatus.COVERED
            elif statuses or requirement.decision_ids:
                requirement.coverage_status = CoverageStatus.PARTIAL
            else:
                requirement.coverage_status = CoverageStatus.GAP
            requirement.solution_element_ids = []
            requirement.acceptance_test_ids = []
            if requirement.coverage_status is not CoverageStatus.GAP:
                object_ref = next((item.object_ref for item in relevant if item.object_ref), "")
                route_ref = next((item.route_ref for item in relevant if item.route_ref), "")
                operational_groups.setdefault(
                    (requirement.cluster, object_ref, route_ref), []
                ).append(requirement)
            if requirement.coverage_status is CoverageStatus.COVERED:
                test_id = stable_id("AT", requirement.id)
                bundle.acceptance_tests.append(
                    AcceptanceTest(
                        id=test_id,
                        preconditions=["Подготовлены подтверждённые настройки, роли и НСИ сценария."],
                        actions=[f"Выполнить бизнес-операцию кластера «{requirement.cluster}» и зафиксировать фактический результат."],
                        expected_result=f"Критерий требования {requirement.id} воспроизводимо выполнен.",
                        requirement_ids=[requirement.id],
                    )
                )
                requirement.acceptance_test_ids = [test_id]
            else:
                critical = any(
                    token in requirement.source_text.casefold()
                    for token in ("оплат", "проведен", "маршрут", "объект", "производ", "отгруз")
                )
                bundle.gaps.append(
                    Gap(
                        id=stable_id("GAP", requirement.id),
                        requirement_ids=[requirement.id],
                        description=f"Нет полного подтверждения: {requirement.source_text}",
                        criticality="critical" if critical else "medium",
                        reason="Доступны только кандидаты либо отсутствует первичный источник точного релиза.",
                        prototype_method="Проверить на тестовой базе точного релиза и сохранить source_ref.",
                        closure_criterion="Получено проверяемое доказательство или пользователь явно исключил требование из границ.",
                    )
                )

        # A user instruction follows real operations, not the sentence count in the source TZ.
        for (cluster, object_ref, route_ref), requirements in operational_groups.items():
            requirement_ids = [item.id for item in requirements]
            group_evidence = [
                item
                for requirement in requirements
                for item in relevant_by_requirement.get(requirement.id, [])
            ]
            evidence_ids = list(dict.fromkeys(item.id for item in group_evidence))
            signature = "|".join((cluster, object_ref, route_ref, *requirement_ids))
            solution_id = stable_id("SOL", signature)
            step_id = stable_id("STEP", signature)
            bundle.solution_elements.append(
                {
                    "id": solution_id,
                    "cluster": cluster,
                    "requirement_ids": requirement_ids,
                    "description": f"Операционный этап «{cluster}»",
                    "requirement_statements": [item.source_text for item in requirements],
                    "evidence_ids": evidence_ids,
                }
            )
            step_status = StepValidationStatus.NEEDS_REVIEW
            if any(item.status is EvidenceStatus.VERIFIED_SOURCE for item in group_evidence):
                step_status = StepValidationStatus.VERIFIED
            elif any(item.status is EvidenceStatus.VERIFIED_METADATA for item in group_evidence):
                step_status = StepValidationStatus.VERIFIED_METADATA
            edge_ref = next((item.edge_ref for item in group_evidence if item.edge_ref), "")
            bundle.instruction_steps.append(
                {
                    "id": step_id,
                    "cluster": cluster,
                    "requirement_ids": requirement_ids,
                    "solution_element_ids": [solution_id],
                    "description": f"Выполнить подтверждённую операцию «{cluster}»",
                    "object_ref": object_ref,
                    "route_ref": route_ref,
                    "edge_ref": edge_ref,
                    "evidence_ids": evidence_ids,
                    "validation_status": step_status.value,
                }
            )
            for requirement in requirements:
                requirement.solution_element_ids = [solution_id]
        steps_by_requirement = {
            req_id: [str(step["id"]) for step in bundle.instruction_steps if req_id in step.get("requirement_ids", [])]
            for req_id in (item.id for item in bundle.requirements)
        }
        bundle.traceability = [
            TraceLink(
                requirement_id=item.id,
                decision_ids=list(item.decision_ids),
                evidence_ids=list(item.evidence_ids),
                solution_element_ids=list(item.solution_element_ids),
                instruction_step_ids=steps_by_requirement[item.id],
                acceptance_test_ids=list(item.acceptance_test_ids),
            )
            for item in bundle.requirements
        ]
        bundle.schema_valid = not validate_bundle(bundle)[0]

    def _analysis_dir(self, project_id: str) -> Path:
        return self.store.project_dir(project_id) / "analysis"

    def _save(self, bundle: AnalysisBundle) -> None:
        atomic_write_json(self._analysis_dir(bundle.project_id) / "analysis.json", bundle.to_dict())

    def _snapshot(self, bundle: AnalysisBundle) -> None:
        atomic_write_json(
            self._analysis_dir(bundle.project_id) / "revisions" / f"r{bundle.revision:04d}.json",
            bundle.to_dict(),
        )

    def _write_artifacts(self, bundle: AnalysisBundle) -> None:
        directory = self._analysis_dir(bundle.project_id)
        payloads = {
            "requirement-map.json": {"requirements": [item.to_dict() for item in bundle.requirements]},
            "evidence-map.json": {"evidence": [item.to_dict() for item in bundle.evidence]},
            "decisions.json": {"decisions": [item.to_dict() for item in bundle.decisions]},
            "gaps.json": {"gaps": [item.to_dict() for item in bundle.gaps]},
            "solution-model.json": {"elements": bundle.solution_elements},
            "traceability.json": {"links": [item.to_dict() for item in bundle.traceability]},
            "acceptance-tests.json": {"tests": [item.to_dict() for item in bundle.acceptance_tests]},
            "questions.json": {"questions": [item.to_dict() for item in bundle.questions]},
            "instruction-contract.json": {
                "project_id": bundle.project_id,
                "revision": bundle.revision,
                "steps": bundle.instruction_steps,
            },
        }
        for name, payload in payloads.items():
            payload["schema_version"] = bundle.schema_version
            payload["project_id"] = bundle.project_id
            payload["revision"] = bundle.revision
            atomic_write_json(directory / name, payload)

    def _write_modeler(self, project_id: str, report: dict[str, Any]) -> None:
        atomic_write_json(self._analysis_dir(project_id) / "modeler-report.json", report)
