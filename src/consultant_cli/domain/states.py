from __future__ import annotations

from consultant_cli.domain.models import Project, ProjectStatus
from consultant_cli.errors import WorkflowBlockedError


ALLOWED_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.CONFIGURED: {
        ProjectStatus.GENERATING,
        ProjectStatus.REQUIREMENTS_PENDING,
        ProjectStatus.ERROR,
    },
    ProjectStatus.REQUIREMENTS_PENDING: {
        ProjectStatus.REQUIREMENTS_APPROVED,
        ProjectStatus.ERROR,
    },
    ProjectStatus.REQUIREMENTS_APPROVED: {
        ProjectStatus.GENERATING,
        ProjectStatus.DESIGN_PENDING,
        ProjectStatus.REQUIREMENTS_PENDING,
        ProjectStatus.ERROR,
    },
    ProjectStatus.DESIGN_PENDING: {
        ProjectStatus.DESIGN_APPROVED,
        ProjectStatus.REQUIREMENTS_APPROVED,
        ProjectStatus.REQUIREMENTS_PENDING,
        ProjectStatus.ERROR,
    },
    ProjectStatus.DESIGN_APPROVED: {
        ProjectStatus.GENERATING,
        ProjectStatus.DESIGN_PENDING,
        ProjectStatus.ERROR,
    },
    ProjectStatus.GENERATING: {
        ProjectStatus.REQUIREMENTS_PENDING,
        ProjectStatus.DESIGN_PENDING,
        ProjectStatus.FEEDBACK_PENDING,
        ProjectStatus.ERROR,
    },
    ProjectStatus.FEEDBACK_PENDING: {
        ProjectStatus.SUCCESSFUL,
        ProjectStatus.NEEDS_REVISION,
        ProjectStatus.DRAFT,
    },
    ProjectStatus.DRAFT: {
        ProjectStatus.GENERATING,
        ProjectStatus.SUCCESSFUL,
        ProjectStatus.NEEDS_REVISION,
    },
    ProjectStatus.SUCCESSFUL: {ProjectStatus.NEEDS_REVISION},
    ProjectStatus.NEEDS_REVISION: {
        ProjectStatus.GENERATING,
        ProjectStatus.REQUIREMENTS_PENDING,
        ProjectStatus.DESIGN_PENDING,
        ProjectStatus.ERROR,
    },
    ProjectStatus.ERROR: {
        ProjectStatus.CONFIGURED,
        ProjectStatus.GENERATING,
        ProjectStatus.NEEDS_REVISION,
    },
}


def transition(project: Project, target: ProjectStatus) -> None:
    if target == project.status:
        return
    if target not in ALLOWED_TRANSITIONS.get(project.status, set()):
        raise WorkflowBlockedError(
            f"Переход {project.status.value} → {target.value} запрещён."
        )
    project.status = target
    project.touch()


def assert_stage_can_be_approved(project: Project, stage: str) -> None:
    expected = {
        "requirements": ProjectStatus.REQUIREMENTS_PENDING,
        "design": ProjectStatus.DESIGN_PENDING,
        "instruction": ProjectStatus.FEEDBACK_PENDING,
    }
    if stage not in expected:
        raise WorkflowBlockedError(f"Неизвестный этап апрува: {stage}")
    if project.status is not expected[stage]:
        raise WorkflowBlockedError(
            f"Нельзя согласовать {stage}: текущий статус {project.status.value}."
        )
