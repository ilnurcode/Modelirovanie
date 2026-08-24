from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from consultant_cli.services.agents import add_common_agent_paths


class AgentPathTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific PATH behavior")
    def test_user_npm_directory_is_added_first_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            appdata = Path(temp)
            npm_bin = appdata / "npm"
            npm_bin.mkdir()
            original_path = os.pathsep.join(["C:\\Tools", str(npm_bin), "C:\\Windows"])

            with patch.dict(
                os.environ,
                {"APPDATA": str(appdata), "PATH": original_path},
                clear=False,
            ):
                add_common_agent_paths()

                parts = os.environ["PATH"].split(os.pathsep)
                self.assertEqual(str(npm_bin), parts[0])
                self.assertEqual(1, parts.count(str(npm_bin)))


if __name__ == "__main__":
    unittest.main()
