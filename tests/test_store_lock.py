from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from consultant_cli.domain.models import Project, ProjectMode
from consultant_cli.errors import WorkflowBlockedError
from consultant_cli.infrastructure.store import ProjectStore, RepositoryPaths
from tests.helpers import make_repository


class ProjectLockTest(unittest.TestCase):
    @staticmethod
    def build(root: Path) -> tuple[ProjectStore, Path]:
        make_repository(root)
        store = ProjectStore(RepositoryPaths(root))
        project = Project("demo", "Demo", ProjectMode.FULL)
        store.create(project, "Тест")
        return store, store.project_dir(project.project_id) / ".project.lock"

    def test_stale_legacy_pid_is_removed_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            store, lock_path = self.build(Path(temp))
            lock_path.write_text("pid=2147483647\n", encoding="utf-8")

            with store.lock("demo"):
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(2, owner["schema_version"])
                self.assertIn("process_start_token", owner)

            self.assertFalse(lock_path.exists())

    def test_live_owner_reports_pid_and_keeps_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            store, lock_path = self.build(Path(temp))

            with store.lock("demo"):
                with self.assertRaisesRegex(WorkflowBlockedError, r"PID \d+"):
                    with store.lock("demo"):
                        pass
                self.assertTrue(lock_path.exists())

            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
