from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


build_application = load_script("build_application", ROOT / "scripts" / "build_application.py")
package_installers = load_script(
    "package_installers", ROOT / "deployment" / "scripts" / "package_installers.py"
)


class DeploymentScriptsTest(unittest.TestCase):
    def test_application_package_excludes_graph_and_verifies_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "root"
            stage = Path(temp) / "stage"
            root.mkdir()
            stage.mkdir()
            (root / "README.md").write_text("readme", encoding="utf-8")
            graphs = root / "1c_modeler_upgrade" / "graphs"
            graphs.mkdir(parents=True)
            (graphs / "large.json").write_text("graph", encoding="utf-8")
            (root / "1c_modeler_upgrade" / "SKILL.md").write_text("skill", encoding="utf-8")

            build_application.copy_application(root, stage)
            (stage / "consultant").write_text("binary", encoding="utf-8")
            build_application.write_integrity(stage)
            archive = Path(temp) / "application.zip"
            with zipfile.ZipFile(archive, "w") as package:
                for path in (item for item in stage.rglob("*") if item.is_file()):
                    package.write(path, path.relative_to(stage).as_posix())

            build_application.verify_archive(archive, "consultant")
            with zipfile.ZipFile(archive) as package:
                self.assertNotIn("1c_modeler_upgrade/graphs/large.json", package.namelist())

    def test_setup_archive_entries_are_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "setup.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package_installers.add_bytes(package, "installer", b"binary", 0o100755)
            with zipfile.ZipFile(archive) as package:
                mode = package.getinfo("installer").external_attr >> 16
            self.assertEqual(0o100755, mode)


if __name__ == "__main__":
    unittest.main()
