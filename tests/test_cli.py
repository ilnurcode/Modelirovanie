from __future__ import annotations

import unittest

from consultant_cli.cli import parser


class CliParserTest(unittest.TestCase):
    def test_new_project_has_no_mode_switch(self):
        args = parser().parse_args(
            ["new", "Закупка", "--prompt", "Создать процесс"]
        )
        self.assertFalse(hasattr(args, "mode"))
        with self.assertRaises(SystemExit):
            parser().parse_args(
                [
                    "new",
                    "Закупка",
                    "--prompt",
                    "Создать процесс",
                    "--mode",
                    "flexible",
                ]
            )

    def test_agent_executable_does_not_replace_top_level_command(self):
        args = parser().parse_args(
            [
                "agent",
                "add",
                "codex-local",
                "--kind",
                "codex_cli",
                "--command",
                "codex",
            ]
        )
        self.assertEqual("agent", args.command)
        self.assertEqual("codex", args.executable_command)


if __name__ == "__main__":
    unittest.main()
