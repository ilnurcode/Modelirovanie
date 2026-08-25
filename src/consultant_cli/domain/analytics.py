from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EvidenceStatus(str, Enum):
    VERIFIED_SOURCE = "verified_source"
    VERIFIED_METADATA = "verified_metadata"
    USER_DECISION = "user_decision"
    CANDIDATE = "candidate"
    UNRESOLVED = "unresolved"


class CoverageStatus(str, Enum):
    COVERED = "covered"
    PARTIAL = "partial"
    GAP = "gap"


class StepValidationStatus(str, Enum):
    VERIFIED = "verified"
    VERIFIED_METADATA = "verified_metadata"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


def stable_id(prefix: str, *parts: str, size: int = 12) -> str:
    normalized = "|".join(
        re.sub(r"\s+", " ", str(part).strip().casefold().replace("ё", "е"))
        for part in parts
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:size]
    return f"{prefix}-{digest}"


@dataclass(slots=True)
class Requirement:
    id: str
    source_text: str
    cluster: str
    source: dict[str, Any]
    coverage_status: CoverageStatus = CoverageStatus.GAP
    decision_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    solution_element_ids: list[str] = field(default_factory=list)
    acceptance_test_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["coverage_status"] = self.coverage_status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Requirement":
        return cls(
            id=str(data["id"]),
            source_text=str(data["source_text"]),
            cluster=str(data["cluster"]),
            source=dict(data.get("source", {})),
            coverage_status=CoverageStatus(data.get("coverage_status", "gap")),
            decision_ids=list(data.get("decision_ids", [])),
            evidence_ids=list(data.get("evidence_ids", [])),
            solution_element_ids=list(data.get("solution_element_ids", [])),
            acceptance_test_ids=list(data.get("acceptance_test_ids", [])),
        )


@dataclass(slots=True)
class Decision:
    id: str
    question_id: str
    exact_user_answer: str
    normalized_value: str
    revision: int
    affected_requirement_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        return cls(**data)


@dataclass(slots=True)
class Evidence:
    id: str
    source_type: str
    product: str
    release: str
    source_ref: str
    excerpt: str
    status: EvidenceStatus
    object_ref: str = ""
    field_ref: str = ""
    route_ref: str = ""
    edge_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        values = dict(data)
        values["status"] = EvidenceStatus(values["status"])
        return cls(**values)


@dataclass(slots=True)
class Gap:
    id: str
    requirement_ids: list[str]
    description: str
    criticality: str
    reason: str
    prototype_method: str
    closure_criterion: str
    status: str = "open"

    @property
    def blocking(self) -> bool:
        return self.criticality == "critical" and self.status not in {"closed", "excluded"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Gap":
        return cls(**data)


@dataclass(slots=True)
class TraceLink:
    requirement_id: str
    decision_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    solution_element_ids: list[str] = field(default_factory=list)
    instruction_step_ids: list[str] = field(default_factory=list)
    acceptance_test_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceLink":
        return cls(**data)


@dataclass(slots=True)
class AcceptanceTest:
    id: str
    preconditions: list[str]
    actions: list[str]
    expected_result: str
    requirement_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AcceptanceTest":
        return cls(**data)


@dataclass(slots=True)
class BusinessQuestion:
    id: str
    text: str
    cluster: str
    requirement_ids: list[str]
    options: list[str] = field(default_factory=list)
    required: bool = True
    follow_up_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BusinessQuestion":
        return cls(**data)


@dataclass(slots=True)
class AnalysisBundle:
    project_id: str
    product: str
    release: str
    source_hash: str
    revision: int = 1
    requirements: list[Requirement] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    traceability: list[TraceLink] = field(default_factory=list)
    acceptance_tests: list[AcceptanceTest] = field(default_factory=list)
    questions: list[BusinessQuestion] = field(default_factory=list)
    solution_elements: list[dict[str, Any]] = field(default_factory=list)
    instruction_steps: list[dict[str, Any]] = field(default_factory=list)
    dirty_clusters: list[str] = field(default_factory=list)
    requirements_approved_revision: int | None = None
    design_approved_revision: int | None = None
    final_approved_revision: int | None = None
    schema_valid: bool = False
    modeler_passed: bool = False
    schema_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "product": self.product,
            "release": self.release,
            "source_hash": self.source_hash,
            "revision": self.revision,
            "requirements": [item.to_dict() for item in self.requirements],
            "decisions": [item.to_dict() for item in self.decisions],
            "evidence": [item.to_dict() for item in self.evidence],
            "gaps": [item.to_dict() for item in self.gaps],
            "traceability": [item.to_dict() for item in self.traceability],
            "acceptance_tests": [item.to_dict() for item in self.acceptance_tests],
            "questions": [item.to_dict() for item in self.questions],
            "solution_elements": self.solution_elements,
            "instruction_steps": self.instruction_steps,
            "dirty_clusters": self.dirty_clusters,
            "approvals": {
                "requirements_revision": self.requirements_approved_revision,
                "design_revision": self.design_approved_revision,
                "final_revision": self.final_approved_revision,
            },
            "checks": {
                "schema_valid": self.schema_valid,
                "modeler_passed": self.modeler_passed,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisBundle":
        approvals = data.get("approvals", {})
        checks = data.get("checks", {})
        return cls(
            project_id=str(data["project_id"]),
            product=str(data.get("product", "")),
            release=str(data.get("release", "")),
            source_hash=str(data.get("source_hash", "")),
            revision=int(data.get("revision", 1)),
            requirements=[Requirement.from_dict(item) for item in data.get("requirements", [])],
            decisions=[Decision.from_dict(item) for item in data.get("decisions", [])],
            evidence=[Evidence.from_dict(item) for item in data.get("evidence", [])],
            gaps=[Gap.from_dict(item) for item in data.get("gaps", [])],
            traceability=[TraceLink.from_dict(item) for item in data.get("traceability", [])],
            acceptance_tests=[AcceptanceTest.from_dict(item) for item in data.get("acceptance_tests", [])],
            questions=[BusinessQuestion.from_dict(item) for item in data.get("questions", [])],
            solution_elements=list(data.get("solution_elements", [])),
            instruction_steps=list(data.get("instruction_steps", [])),
            dirty_clusters=list(data.get("dirty_clusters", [])),
            requirements_approved_revision=approvals.get("requirements_revision"),
            design_approved_revision=approvals.get("design_revision"),
            final_approved_revision=approvals.get("final_revision"),
            schema_valid=bool(checks.get("schema_valid", False)),
            modeler_passed=bool(checks.get("modeler_passed", False)),
            schema_version=str(data.get("schema_version", "1.0.0")),
        )
