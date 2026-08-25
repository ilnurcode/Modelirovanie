from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from consultant_cli.services.agents import add_common_agent_paths
from consultant_cli.infrastructure.settings import AppSettings
from consultant_cli.infrastructure.store import RepositoryPaths
from consultant_cli.services.agents import AgentService, extract_json
from tests.helpers import make_repository


class AgentPathTest(unittest.TestCase):
    def test_single_missing_json_comma_is_repaired_locally(self):
        self.assertEqual(
            {"first": 1, "second": 2},
            extract_json('{"first": 1 "second": 2}'),
        )

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

    def test_role_policy_uses_required_models_and_local_dotenv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_repository(root)
            (root / "agent-runtime-policy.json").write_text(
                '{"allowed_agents":["erp-translator","erp-process-planner","instruction-writer"],'
                '"api_key_precedence":["WORMSOFT_API_KEY"],'
                '"api_key_files":[".env"],'
                '"models_by_agent":{"erp-translator":"wormsoft/agent/low",'
                '"erp-process-planner":"wormsoft/agent/medium",'
                '"instruction-writer":"wormsoft/agent/high"}}',
                encoding="utf-8",
            )
            (root / ".env").write_text("WORMSOFT_API_KEY=test-secret\n", encoding="utf-8")
            service = AgentService(RepositoryPaths(root), AppSettings(), root / "local.toml")
            self.assertEqual("wormsoft/agent/low", service.role_profile("erp-translator").model)
            self.assertEqual("wormsoft/agent/medium", service.role_profile("erp-process-planner").model)
            self.assertEqual("wormsoft/agent/high", service.role_profile("instruction-writer").model)
            status = service.api_runtime_status()
            self.assertTrue(status["key_configured"])
            self.assertNotIn("test-secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
