from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consultant_cli.infrastructure.settings import (
    AgentProfile,
    AppSettings,
    load_settings,
    save_settings,
)


class SettingsTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "consultant.local.toml"
            settings = AppSettings(
                default_agent="codex-local",
                preferred_interface="codex",
                agents={
                    "codex-local": AgentProfile(
                        name="codex-local", kind="codex_cli", command="codex"
                    )
                },
            )
            save_settings(path, settings)
            loaded = load_settings(path)
            self.assertEqual("codex-local", loaded.default_agent)
            self.assertEqual("codex", loaded.preferred_interface)
            self.assertEqual("codex_cli", loaded.agents["codex-local"].kind)


if __name__ == "__main__":
    unittest.main()
