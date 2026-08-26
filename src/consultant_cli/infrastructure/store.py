from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
import time
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


def _process_start_token(pid: int) -> str | None:
    """Return an OS process creation token, or None when the process is absent."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied means the process exists but cannot be inspected.
            return "alive-access-denied" if ctypes.get_last_error() == 5 else None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return "alive-unknown-start"
            return f"{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
        finally:
            kernel32.CloseHandle(handle)
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            return proc_stat.read_text(encoding="ascii").split()[21]
        except (OSError, IndexError):
            return "alive-unknown-start"
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return None
    return "alive-unknown-start"


def _read_lock_owner(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        legacy = re.fullmatch(r"pid=(\d+)", raw)
        return {"pid": int(legacy.group(1)), "legacy": True} if legacy else {}


def _lock_owner_is_alive(owner: dict[str, Any]) -> bool:
    try:
        pid = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    current_token = _process_start_token(pid)
    if current_token is None:
        return False
    saved_token = str(owner.get("process_start_token") or "")
    if saved_token and current_token not in {
        saved_token,
        "alive-access-denied",
        "alive-unknown-start",
    }:
        # The PID was reused by a different process after a crash/reboot.
        return False
    return True


def slugify(value: str) -> str:
    value = value.casefold().translate(TRANSLIT)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:64] or "project"


def _replace_with_retry(source: str, target: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as stream:
        stream.write(text)
        temp_name = stream.name
    try:
        _replace_with_retry(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
        stream.write(data)
        temp_name = stream.name
    try:
        _replace_with_retry(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class RepositoryPaths:
    def __init__(self, root: Path):
        self.root = root.resolve()
        data_dir = os.environ.get("CONSULTANT_DATA_DIR")
        self.data_root = Path(data_dir).resolve() if data_dir else self.root
        self.results = self.data_root / "results"
        self.project_trash = self.results / ".trash"
        self.examples_index = self.data_root / "examples" / "approved" / "index.ndjson"
        self.local_config = (
            self.data_root / "config" / "consultant.local.toml"
            if data_dir else self.root / "consultant.local.toml"
        )

    @classmethod
    def discover(cls, start: Path | None = None) -> "RepositoryPaths":
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            if (candidate / "skills").is_dir() and (candidate / "README.md").is_file():
                return cls(candidate)
        raise NotFoundError(
            "Не найден корень базы знаний: ожидаются README.md и каталог skills/."
        )

    def installed_graphs(self) -> list[Path]:
        state_path = self.data_root / "config" / "installed.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            records = sorted(
                state.get("graphs", {}).values(),
                key=lambda graph: str(graph.get("installed_at", "")),
                reverse=True,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []
        graph_root = (self.data_root / "graphs").resolve()
        result = []
        for graph in records:
            path = Path(str(graph.get("path", ""))).resolve()
            if graph_root in path.parents and path.is_dir():
                result.append(path)
        return result

    def modeler_graphs(self) -> Path:
        installed = self.installed_graphs()
        return installed[0] if installed else self.root / "1c_modeler_upgrade" / "graphs"


class ProjectStore:
    def __init__(self, paths: RepositoryPaths):
        self.paths = paths
        self.paths.results.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        safe_id = slugify(project_id)
        raw = str(project_id).strip()
        if (
            not raw
            or Path(raw).name != raw
            or raw in {".", ".."}
            or any(character in raw for character in '\\/:*?"<>|')
        ):
            raise NotFoundError(f"Некорректный project-id: {project_id}")
        # New projects use ASCII slugs. Existing v1-v3 projects may have safe
        # Cyrillic directory names and must remain readable for revisions/export.
        existing = self.paths.results / raw
        if safe_id != raw and not existing.is_dir():
            raise NotFoundError(f"Некорректный project-id: {project_id}")
        path = existing.resolve()
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
        lock_path = directory / ".project.lock"
        owner = _read_lock_owner(lock_path) if lock_path.exists() else {}
        if lock_path.exists() and _lock_owner_is_alive(owner):
            raise WorkflowBlockedError(
                self._active_lock_message(project_id, owner)
            )
        if lock_path.exists():
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
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
        descriptor: int | None = None
        for _attempt in range(2):
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                owner = _read_lock_owner(path)
                if owner and _lock_owner_is_alive(owner):
                    raise WorkflowBlockedError(
                        self._active_lock_message(project_id, owner)
                    ) from exc
                # Do not remove a just-created file before its owner writes JSON.
                try:
                    fresh_unknown = not owner and (
                        datetime.now().timestamp() - path.stat().st_mtime < 5
                    )
                except FileNotFoundError:
                    fresh_unknown = False
                if fresh_unknown:
                    raise WorkflowBlockedError(
                        f"Проект {project_id} только что начал изменяться другим процессом."
                    ) from exc
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        if descriptor is None:
            raise WorkflowBlockedError(
                f"Не удалось получить блокировку проекта {project_id}."
            )
        nonce = uuid.uuid4().hex
        owner = {
            "schema_version": 2,
            "pid": os.getpid(),
            "process_start_token": _process_start_token(os.getpid()),
            "created_at": now_iso(),
            "host": socket.gethostname(),
            "command": Path(sys.argv[0]).name,
            "nonce": nonce,
        }
        try:
            os.write(
                descriptor,
                (json.dumps(owner, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
            )
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                current = _read_lock_owner(path)
                if current.get("nonce") == nonce:
                    path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _active_lock_message(project_id: str, owner: dict[str, Any]) -> str:
        details = [f"PID {owner.get('pid', '?')}"]
        if owner.get("command"):
            details.append(str(owner["command"]))
        if owner.get("created_at"):
            details.append(f"с {owner['created_at']}")
        return f"Проект {project_id} уже изменяется процессом " + ", ".join(details) + "."
