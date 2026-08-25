from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from consultant_cli.cli import parser, read_markdown_input
from consultant_cli.errors import InvalidConfigurationError


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

    def test_new_project_accepts_markdown_file_instead_of_prompt(self):
        args = parser().parse_args(["new", "Проект", "--file", "tz.md"])
        self.assertEqual(Path("tz.md"), args.file)
        self.assertIsNone(args.prompt)
        with self.assertRaises(SystemExit):
            parser().parse_args(
                ["new", "Проект", "--file", "tz.md", "--prompt", "Текст"]
            )

    def test_markdown_input_is_utf8_and_preserves_source_name(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "тз.md"
            source.write_text("# Техническое задание\n", encoding="utf-8")
            text, name, raw = read_markdown_input(source)
            self.assertEqual(source.read_bytes().decode("utf-8"), text)
            self.assertEqual("тз.md", name)
            self.assertEqual(source.read_bytes(), raw)
            invalid = Path(temp) / "tz.txt"
            invalid.write_text("text", encoding="utf-8")
            with self.assertRaises(InvalidConfigurationError):
                read_markdown_input(invalid)


if __name__ == "__main__":
    unittest.main()
