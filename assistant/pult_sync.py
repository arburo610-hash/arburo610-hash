"""Safely mirror confirmed project-stage facts to an isolated copy of the Pult.

Pilot scope: one project, one field ('Текущий этап / период'), explicit approval.
Never write to the actual Pult or change project counts, payments, or dates.
"""
import os
from pydantic import BaseModel
import sheets

TEST_PROJECT_ID = "1xPpsq2TLWRPqFMtRza2XraVvUKMZOuZw6VY1u2_YvIw"
TEST_PULT_ID = "1a5ZQWEG6EB576NO8XYxXZ2zMTvYY8p4XYSbctQayF6M"
PROJECT_NAME = "ЖК МОД"
PULT_COLUMN = "Текущий этап / период"


class PultProposal(BaseModel):
    project: str
    sheet_id: str
    test_project_id: str
    expected: str
    value: str


def _pilot_project():
    project = sheets.find_project(PROJECT_NAME)
    if not project or project.get("Проект") != PROJECT_NAME:
        raise ValueError("Пилотный проект не найден или имя отличается")
    if sheets._test_override_id(project) != TEST_PROJECT_ID:
        raise PermissionError("Доступ к проверенной тестовой копии проекта не настроен")
    return project


def _test_pult():
    configured = os.environ.get("TEST_PULT_SPREADSHEET_ID", "").strip()
    if not configured:
        raise PermissionError(
            "Тестовый сервис Railway не видит TEST_PULT_SPREADSHEET_ID. "
            "Проверь, что переменная сохранена в vigilant-rebirth и применено новое развёртывание"
        )
    if configured != TEST_PULT_ID:
        raise PermissionError(
            "Значение TEST_PULT_SPREADSHEET_ID не совпадает с проверенной тестовой копией. "
            "Укажи только ID таблицы, без ссылки, кавычек или пробелов внутри"
        )
    if configured == sheets.PULT_ID:
        raise PermissionError("Запрещено использовать рабочий Пульт для тестовой записи")
    return sheets._gc().open_by_key(configured).worksheet(sheets.PULT_SHEET)


def _row_for_project(ws):
    values = ws.get_all_values()
    if not values or "Проект" not in values[0] or PULT_COLUMN not in values[0]:
        raise ValueError("Заголовки Пульта не соответствуют ожидаемым")
    name_col = values[0].index("Проект")
    found = [i for i, row in enumerate(values[1:], start=1)
             if len(row) > name_col and row[name_col].strip() == PROJECT_NAME]
    if len(found) != 1:
        raise ValueError("Нужна ровно одна строка ЖК МОД в тестовом Пульте")
    row = values[found[0]]
    col = values[0].index(PULT_COLUMN)
    return found[0] + 1, col + 1, row[col] if len(row) > col else ""


def _stage_rows():
    _, source_id, ws = sheets._project_sheet(PROJECT_NAME, "этапы")
    if source_id != TEST_PROJECT_ID:
        raise PermissionError("В работе неожиданно оказалась НЕ тестовая таблица проекта")
    grid = ws.get_all_values()
    if not grid:
        raise ValueError("Лист этапов пуст")
    headings = [x.strip() for x in grid[0]]
    keys = ("№", "Статус выполнения работ", "Акт")
    if not all(k in headings for k in keys):
        raise ValueError("В тестовой таблице отсутствует статус выполнения работ или акт")
    n_col = headings.index("№")
    rows = {}
    for row in grid[1:]:
        if len(row) > n_col and row[n_col].strip() in ("1", "2"):
            rows[row[n_col].strip()] = {
                k: row[headings.index(k)].strip() if len(row) > headings.index(k) else ""
                for k in keys
            }
    if set(rows) != {"1", "2"}:
        raise ValueError("Не найдены этапы 1 и 2")
    return rows


def draft():
    _pilot_project()
    stages = _stage_rows()
    first, second = stages["1"], stages["2"]
    if not first["Статус выполнения работ"]:
        raise ValueError("Нужно сперва подтвердить фактический статус работ этапа 1")
    if not second["Статус выполнения работ"]:
        raise ValueError(
            "Статус работ этапа 2 в тестовой таблице пока пуст. "
            "Сначала зафиксируй его через /обновить МОД; иначе Пульт снова будет неточным."
        )
    if first["Статус выполнения работ"] != "Работы выполнены":
        raise ValueError("Пилот рассчитан на этап 1 с подтверждённым выполнением работ")
    if second["Статус выполнения работ"] != "В работе":
        raise ValueError("Не подтверждена работа над вторым этапом")
    signed = first["Акт"] == "Подписан"
    status = "акт подписан" if signed else "акт не подписан"
    value = f"Этап 2: эскизы в работе. Этап 1: работы выполнены, {status}."
    ws = _test_pult()
    _, _, actual = _row_for_project(ws)
    if actual == value:
        raise ValueError("Тестовый Пульт уже содержит актуальную сводку")
    return PultProposal(
        project=PROJECT_NAME, sheet_id=TEST_PULT_ID,
        test_project_id=TEST_PROJECT_ID, expected=actual, value=value,
    )


def apply(proposal: PultProposal):
    """Explicit opt-in and a compare-and-set style check before a single-cell write."""
    if os.environ.get("PULT_SYNC_ENABLED", "false").lower() != "true":
        raise PermissionError("Синхронизация Пульта пока отключена")
    if proposal.sheet_id != TEST_PULT_ID or proposal.test_project_id != TEST_PROJECT_ID:
        raise PermissionError("Предложение не относится к проверенным тестовым таблицам")
    ws = _test_pult()
    current = draft()  # Refresh stage facts and Pult value to detect stale approval.
    if current.expected != proposal.expected or current.value != proposal.value:
        raise ValueError("Данные изменились после подготовки предложения: создай новый черновик")
    row, col, actual = _row_for_project(ws)
    if actual != proposal.expected:
        raise ValueError("Значение Пульта изменилось; перезапись запрещена")
    ws.update_cell(row, col, proposal.value)
    if (ws.cell(row, col).value or "") != proposal.value:
        raise RuntimeError("Google Sheets не подтвердил обновление тестового Пульта")
    return f"{PROJECT_NAME}: {PULT_COLUMN}"
