"""Ensure a brief never invents tasks for inaccessible projects."""
import sys
import importlib.util
from unittest.mock import MagicMock
import unittest
from pathlib import Path
from unittest.mock import patch

# GitHub offline checks do not install runtime Google, audio or Pydantic deps.
# Load the isolated preflight module with mocks for its external dependencies.
module_path = Path(__file__).resolve().parents[1] / "brief_snapshot.py"
spec = importlib.util.spec_from_file_location("brief_snapshot_unit", module_path)
brief_snapshot = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"reminders": MagicMock(), "sheets": MagicMock()}):
    spec.loader.exec_module(brief_snapshot)


class BriefSnapshotTests(unittest.TestCase):
    def test_inaccessible_project_is_marked_not_assumed_empty(self):
        projects = [
            {"Проект": "ЖК МОД", "Статус": "В работе", "Текущий этап / период": "Этап 2"},
            {"Проект": "ЖК ОСТРОВ", "Статус": "Ожидание подписания и оплаты",
             "Активные задачи": "5"},
        ]
        def fake_read(name, section, only_open=False):
            if name == "ЖК ОСТРОВ":
                raise PermissionError("permission denied")
            return {"записи": [{"№": "2", "Статус выполнения работ": "В работе"}]}
        with patch.object(brief_snapshot.sheets, "list_projects", return_value=projects), \
             patch.object(brief_snapshot.sheets, "pult_read_source", return_value="ТЕСТОВЫЙ Пульт"), \
             patch.object(brief_snapshot.sheets, "read_project_section", side_effect=fake_read), \
             patch.object(brief_snapshot.reminders, "find", return_value=[]):
            snapshot = brief_snapshot.gather()
        island = snapshot["проекты"][1]
        self.assertIn("неполный", island["доступ"])
        self.assertEqual(island["разделы"]["задачи"]["записи"], [])
        self.assertEqual(island["разделы"]["задачи"]["состояние"], "недоступен")
        self.assertEqual(island["сведения_пульта"]["Активные задачи"], "5")
        self.assertIn("ТЕСТОВЫЙ БРИФ", brief_snapshot.prompt(snapshot))

    def test_old_today_text_is_not_due_today(self):
        today = __import__("datetime").date(2026, 11, 4)
        for task_id, old_due in [
            ("MOD-T-012", "28.09.2026"),
            ("MOD-T-017", "02.10.2026"),
            ("TASK-021", "26.10–01.11.2026")
        ]:
            task = brief_snapshot._annotate_task({
                "ID": task_id, "Срок": old_due,
                "Следующий шаг": "Провести созвон сегодня"
            }, today)
            self.assertFalse(task["Срок приходится на дату брифа"])
            self.assertIn("В ПРОШЛОМ", task["Проверка даты для брифа"])
            self.assertIn("историческое", task["Предупреждение о старой формулировке"])

    def test_current_explicit_date_can_be_due_today(self):
        today = __import__("datetime").date(2026, 11, 4)
        task = brief_snapshot._annotate_task({
            "ID": "X", "Срок": "04.11.2026", "Следующий шаг": "Проверить сегодня"
        }, today)
        self.assertTrue(task["Срок приходится на дату брифа"])
        self.assertNotIn("Предупреждение о старой формулировке", task)

    def test_missing_date_is_not_due_today(self):
        today = __import__("datetime").date(2026, 11, 4)
        task = brief_snapshot._annotate_task({
            "ID": "X", "Срок": "", "Следующий шаг": "Сделать сегодня"
        }, today)
        self.assertFalse(task["Срок приходится на дату брифа"])

    def test_completed_project_not_queried(self):
        with patch.object(brief_snapshot.sheets, "list_projects", return_value=[
            {"Проект": "ЖК Талисман", "Статус": "Завершён"}
        ]), patch.object(brief_snapshot.sheets, "pult_read_source", return_value="ТЕСТОВЫЙ Пульт"), \
             patch.object(brief_snapshot.sheets, "read_project_section") as read, \
             patch.object(brief_snapshot.reminders, "find", return_value=[]):
            result = brief_snapshot.gather()
        read.assert_not_called()
        self.assertIn("не проверялся", result["проекты"][0]["доступ"])


if __name__ == "__main__":
    unittest.main()
