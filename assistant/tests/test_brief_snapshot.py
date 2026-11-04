"""Ensure a brief never invents tasks for inaccessible projects."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brief_snapshot


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
