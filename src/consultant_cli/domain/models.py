from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
APP_TIMEZONE = timezone(timedelta(hours=7), name="Asia/Tomsk")


def now_iso() -> str:
    return datetime.now(APP_TIMEZONE).isoformat(timespec="seconds")


class ProjectMode(str, Enum):
    FULL = "full"


class ProjectStatus(str, Enum):
    CONFIGURED = "configured"
    REQUIREMENTS_PENDING = "requirements_pending"
    REQUIREMENTS_APPROVED = "requirements_approved"
    DESIGN_PENDING = "design_pending"
    DESIGN_APPROVED = "design_approved"
    GENERATING = "generating"
    FEEDBACK_PENDING = "feedback_pending"
    DRAFT = "draft"
    SUCCESSFUL = "successful"
    NEEDS_REVISION = "needs_revision"
    ERROR = "error"


def project_state(status: ProjectStatus) -> str:
    """Return the consultant-facing lifecycle group for a project."""
    if status is ProjectStatus.SUCCESSFUL:
        return "confirmed"
    if status in {ProjectStatus.FEEDBACK_PENDING, ProjectStatus.DRAFT}:
        return "unconfirmed"
    return "in_development"


@dataclass(slots=True)
class ConfigurationInfo:
    product: str = "not_configured"
    edition: str = ""
    release: str = ""

    @property
    def is_unspecified(self) -> bool:
        return not self.product.strip() or self.product.strip().casefold() in {
            "not_configured",
            "не указана",
        }


@dataclass(slots=True)
class GenerationSettings:
    questions: str = "required"
    follow_up_questions: bool = True
    diagram: bool = True
    detail_level: str = "balanced"
    deliverable: str = "hybrid"


@dataclass(slots=True)
class SourceSettings:
    internet_policy: str = "official_and_allowed_web"
    local_configuration_id: str = ""


@dataclass(slots=True)
class Project:
    project_id: str
    title: str
    mode: ProjectMode
    status: ProjectStatus = ProjectStatus.CONFIGURED
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    configuration: ConfigurationInfo = field(default_factory=ConfigurationInfo)
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    sources: SourceSettings = field(default_factory=SourceSettings)
    agent_profile: str = ""
    revision: int = 1
    requirements_version: int = 0
    design_version: int = 0
    instruction_version: int = 0
    last_error: str = ""

    def __post_init__(self) -> None:
        self.generation.questions = "required"

    def touch(self) -> None:
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mode"] = self.mode.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            project_id=str(data["project_id"]),
            title=str(data.get("title", data["project_id"])),
            mode=ProjectMode.FULL,
            status=ProjectStatus(data.get("status", ProjectStatus.CONFIGURED.value)),
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
            configuration=ConfigurationInfo(**data.get("configuration", {})),
            generation=GenerationSettings(**data.get("generation", {})),
            sources=SourceSettings(**data.get("sources", {})),
            agent_profile=str(data.get("agent_profile", "")),
            revision=int(data.get("revision", 1)),
            requirements_version=int(data.get("requirements_version", 0)),
            design_version=int(data.get("design_version", 0)),
            instruction_version=int(data.get("instruction_version", 0)),
            last_error=str(data.get("last_error", "")),
        )
