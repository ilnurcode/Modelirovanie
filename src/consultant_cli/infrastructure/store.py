from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from consultant_cli.domain.models import APP_TIMEZONE, Project, now_iso
from consultant_cli.errors import NotFoundError, WorkflowBlockedError
from consultant_cli.infrastructure import yamlio


TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)


def slugify(value: str) -> str:
    value = value.casefold().translate(TRANSLIT)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:64] or "project"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as stream:
        stream.write(text)
        temp_name = stream.name
    os.replace(temp_name, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class RepositoryPaths:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.results = self.root / "results"
        self.project_trash = self.results / ".trash"
        self.examples_index = self.root / "examples" / "approved" / "index.ndjson"
        self.local_config = self.root / "consultant.local.toml"

    @classmethod
    def discover(cls, start: Path | None = None) -> "RepositoryPaths":
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            if (candidate / "skills").is_dir() and (candidate / "README.md").is_file():
                return cls(candidate)
        raise NotFoundError(
            "Не найден корень базы знаний: ожидаются README.md и каталог skills/."
        )


class ProjectStore:
    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.paths.results.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        safe_id = slugify(project_id)
        if safe_id != project_id:
            raise NotFoundError(f"Некорректный project-id: {project_id}")
        path = (self.paths.results / safe_id).resolve()
        if self.paths.results.resolve() not in path.parents:
            raise NotFoundError("Путь проекта выходит за results/.")
        return path

    def create(self, project: Project, prompt: str) -> Path:
        directory = self.project_dir(project.project_id)
        if directory.exists():
            raise WorkflowBlockedError(f"Проект уже существует: {project.project_id}")
        directory.mkdir(parents=True)
        self.save(project)
        request = (
            "---\n"
            f'artifact: "request"\nproject_id: "{project.project_id}"\n'
            f'created_at: "{project.created_at}"\n---\n\n'
            f"# Исходный запрос: {project.title}\n\n{prompt.strip()}\n"
        )
        atomic_write_text(directory / "00-request.md", request)
        self.append_event(project.project_id, "project_created", {"mode": project.mode.value})
        return directory

    def save(self, project: Project) -> None:
        project.touch()
        path = self.project_dir(project.project_id) / "project.yaml"
        atomic_write_text(path, yamlio.dumps(project.to_dict()))

    def load(self, project_id: str) -> Project:
        path = self.project_dir(project_id) / "project.yaml"
        if not path.is_file():
            raise NotFoundError(f"Проект не найден: {project_id}")
        return Project.from_dict(yamlio.load(path))

    def list(self) -> list[Project]:
        projects = []
        for path in sorted(self.paths.results.glob("*/project.yaml")):
            try:
                projects.append(Project.from_dict(yamlio.load(path)))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(projects, key=lambda project: project.updated_at, reverse=True)

    def trash(self, project_id: str) -> Path:
        """Remove a project from active lists by moving it to recoverable trash."""
        project = self.load(project_id)
        directory = self.project_dir(project_id)
        if (directory / ".project.lock").exists():
            raise WorkflowBlockedError(
                f"Проект {project_id} сейчас изменяется другим процессом."
            )
        self.append_event(
            project_id,
            "project_moved_to_trash",
            {"previous_status": project.status.value},
        )
        self.paths.project_trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(APP_TIMEZONE).strftime("%Y%m%dT%H%M%S%f")
        destination = (self.paths.project_trash / f"{project_id}-{stamp}").resolve()
        if self.paths.project_trash.resolve() not in destination.parents:
            raise WorkflowBlockedError("Путь корзины выходит за results/.trash/.")
        if destination.exists():
            destination = self.paths.project_trash / f"{project_id}-{stamp}-{uuid.uuid4().hex[:8]}"
        os.replace(directory, destination)
        return destination

    def artifact_path(self, project_id: str, name: str) -> Path:
        if Path(name).name != name:
            raise WorkflowBlockedError("Имя артефакта не должно содержать путь.")
        return self.project_dir(project_id) / name

    def read_artifact(self, project_id: str, name: str) -> str:
        path = self.artifact_path(project_id, name)
        if not path.is_file():
            raise NotFoundError(f"Артефакт не найден: {name}")
        return path.read_text(encoding="utf-8")

    def write_artifact(self, project_id: str, name: str, text: str) -> Path:
        path = self.artifact_path(project_id, name)
        if path.exists():
            self._archive(project_id, path)
        atomic_write_text(path, text)
        return path

    def _archive(self, project_id: str, path: Path) -> None:
        stamp = datetime.now(APP_TIMEZONE).strftime("%Y%m%dT%H%M%S%f")
        archive = self.project_dir(project_id) / "revisions" / stamp / path.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(archive, path.read_text(encoding="utf-8"))

    def append_event(
        self, project_id: str, event_type: str, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        event = {
            "event_id": str(uuid.uuid4()),
            "created_at": now_iso(),
            "type": event_type,
            "details": details or {},
        }
        path = self.project_dir(project_id) / "events.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def read_events(self, project_id: str) -> list[dict[str, Any]]:
        path = self.project_dir(project_id) / "events.ndjson"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    @contextmanager
    def lock(self, project_id: str) -> Iterator[None]:
        path = self.project_dir(project_id) / ".project.lock"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise WorkflowBlockedError(
                f"Проект {project_id} уже изменяется другим процессом."
            ) from exc
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
