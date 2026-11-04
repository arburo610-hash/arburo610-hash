"""Offline contracts for isolated stage planning, without Google runtime deps."""
import datetime
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

pyd = types.ModuleType("pydantic")
pyd.BaseModel = object
spec = importlib.util.spec_from_file_location(
    "stage_plan_pilot_test", Path(__file__).resolve().parents[1] / "stage_plan.py"
)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"pydantic": pyd, "sheets": MagicMock()}):
    spec.loader.exec_module(module)


class StagePlanTests(unittest.TestCase):
    def test_explicit_dates_only(self):
        self.assertEqual(module.parse_date("10.11.2026"), datetime.date(2026, 11, 10))
        for raw in ("завтра", "примерно 10 ноября", "31.02.2027", "2026-11-10", ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                module.parse_date(raw)

    def test_find_stage_by_actual_stage_number(self):
        ws = MagicMock()
        ws.get_all_values.return_value = [
            ["№", "Этап", "План: старт", "План: завершение"],
            ["1", "ТЗ", "", ""], ["2", "Эскизы", "", ""],
            ["3", "Заказная мебель", "", ""]
        ]
        row, name, start, end, col_a, col_b = module._stage_row(ws, 2)
        self.assertEqual((row, name, start, end, col_a, col_b),
                         (3, "Эскизы", "", "", 3, 4))

    def test_gantt_span_crosses_year_boundary(self):
        ws = MagicMock()
        start_date = datetime.date(2026, 12, 28)
        origin = datetime.date(1899, 12, 30)
        weeks = [start_date + datetime.timedelta(weeks=i) for i in range(14)]
        ws.get.return_value = [[(day - origin).days for day in weeks]]
        marks = module._gantt_marks(ws, datetime.date(2027, 1, 2),
                                    datetime.date(2027, 1, 12))
        self.assertEqual(marks[:3], ["■", "■", "■"])
        self.assertEqual(marks[3:], [""] * 11)

    def test_guard_refuses_wrong_pult(self):
        with patch.dict(module.os.environ, {"TEST_PULT_SPREADSHEET_ID": "wrong"}, clear=True):
            with self.assertRaises(PermissionError):
                module._guard()


if __name__ == "__main__":
    unittest.main()
