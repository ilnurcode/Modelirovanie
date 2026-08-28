from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from consultant_cli.console import interface_available, open_external_agent


class ExternalInterfaceTest(unittest.TestCase):
    def app(self, root: Path):
        return SimpleNamespace(
            paths=SimpleNamespace(root=root, data_root=root.parent.parent),
            store=SimpleNamespace(load=Mock()),
        )

    def test_codex_uses_desktop_app_and_service_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app" / "4.2.1"
            root.mkdir(parents=True)
            app = self.app(root)
            (app.paths.data_root / "AGENTS.md").write_text("instructions", encoding="utf-8")
            with patch("consultant_cli.console.shutil.which", return_value="C:/codex.exe"):
                command = open_external_agent(app, "demo", "codex")
            self.assertEqual(["C:/codex.exe", "app", str(app.paths.data_root)], command)

    def test_opencode_opens_service_root(self):
        root = Path("C:/service/app/4.2.1")
        with patch("consultant_cli.console.shutil.which", return_value="C:/opencode.exe"):
            command = open_external_agent(self.app(root), "demo", "opencode")
        self.assertEqual(["C:/opencode.exe", str(root)], command)

    @patch("consultant_cli.console.subprocess.run")
    @patch("consultant_cli.console.shutil.which", return_value="C:/opencode.exe")
    def test_launch_passes_selected_project_and_data_root(self, _which, run):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app" / "4.2.1"
            root.mkdir(parents=True)
            app = self.app(root)
            open_external_agent(app, "demo", "opencode", launch=True)
            app.store.load.assert_called_once_with("demo")
            env = run.call_args.kwargs["env"]
            self.assertEqual("demo", env["CONSULTANT_PROJECT_ID"])
            self.assertEqual(str(root.parent.parent), env["CONSULTANT_DATA_DIR"])
            self.assertEqual(
                "demo",
                (app.paths.data_root / "config" / "selected-project.txt")
                .read_text(encoding="utf-8")
                .strip(),
            )


if __name__ == "__main__":
    unittest.main()
