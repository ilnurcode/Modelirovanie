from __future__ import annotations

import io
import unittest

from consultant_cli.ui import ConsoleUI


class ConsoleUiTest(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.ui = ConsoleUI(self.stream, color=False)

    def test_menu_has_titles_and_descriptions(self):
        self.ui.menu([("1", "Новый проект", "Создать инструкцию"), ("0", "Выход", "")])
        text = self.stream.getvalue()
        self.assertIn("Новый проект", text)
        self.assertIn("Создать инструкцию", text)
        self.assertIn("Выход", text)

    def test_table_truncates_long_values(self):
        self.ui.table(["ID", "Название"], [["one", "Очень длинное название проекта " * 5]])
        text = self.stream.getvalue()
        self.assertIn("ID", text)
        self.assertIn("…", text)

    def test_status_has_human_readable_label(self):
        self.assertEqual("ожидается оценка", self.ui.status_text("feedback_pending"))

    def test_project_state_has_clear_business_label(self):
        self.assertEqual("в разработке", self.ui.project_state_text("in_development"))
        self.assertEqual("не подтверждён", self.ui.project_state_text("unconfirmed"))
        self.assertEqual("подтверждён", self.ui.project_state_text("confirmed"))


if __name__ == "__main__":
    unittest.main()
