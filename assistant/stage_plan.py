"""Pilot stage planning: explicit dates, separate approval, test copies only.

The stage table is the source of truth. Gantt is a derived snapshot, and
a failed refresh must be reported for manual reconciliation, never replayed
automatically.
"""
import datetime
import os
import re
from pydantic import BaseModel
import sheets

PROJECT_ID = "1xPpsq2TLWRPqFMtRza2XraVvUKMZOuZw6VY1u2_YvIw"
PULT_ID = "1a5ZQWEG6EB576NO8XYxXZ2zMTvYY8p4XYSbctQayF6M"
PROJECT = "ЖК МОД"
GANTT = "Гант | ТЕСТ"
STAGES = "02 Этапы"
START_COL = "План: старт"
END_COL = "План: завершение"
D_FMT = "%d.%m.%Y"


class PlanProposal(BaseModel):
    project: str
    project_id: str
    pult_id: str
    stage: int
    stage_name: str
    old_start: str
    old_end: str
    new_start: str
    new_end: str


def parse_date(text):
    raw = str(text or "").strip()
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw):
        raise ValueError("Нужна точная дата ДД.ММ.ГГГГ, без слов «примерно» или «на следующей неделе»")
    try:
        return datetime.datetime.strptime(raw, D_FMT).date()
    except ValueError as exc:
        raise ValueError("Некорректная календарная дата") from exc


def _guard():
    if os.environ.get("TEST_PULT_SPREADSHEET_ID", "").strip() != PULT_ID:
        raise PermissionError("Нужен проверенный тестовый Пульт")
    if sheets.PULT_ID == PULT_ID:
        raise PermissionError("Нельзя заменять рабочий Пульт на тестовый ID")
    project = sheets.find_project(PROJECT)
    if not project or project.get("Проект") != PROJECT:
        raise ValueError("ЖК МОД не найден в Пульте")
    if sheets._test_override_id(project) != PROJECT_ID:
        raise PermissionError("Нужна тестовая копия проекта ЖК МОД")
    sheets._assert_test_write_allowed(PROJECT_ID)
    if os.environ.get("PULT_SYNC_ENABLED", "").lower() != "true":
        raise PermissionError("Обновление тестового Ганта отключено")


def _sheets():
    client = sheets._gc()
    return client.open_by_key(PROJECT_ID).worksheet(STAGES), client.open_by_key(PULT_ID).worksheet(GANTT)


def _stage_row(ws, stage):
    grid = ws.get_all_values()
    if not grid or not {"№", "Этап", START_COL, END_COL}.issubset(set(grid[0])):
        raise ValueError("Столбцы листа этапов изменились")
    headers = grid[0]
    col_num = headers.index("№")
    matches = [(i + 1, r) for i, r in enumerate(grid[1:], start=1)
               if len(r) > col_num and r[col_num].strip() == str(stage)]
    if len(matches) != 1:
        raise ValueError("Этап не найден или его номер повторяется")
    i, row = matches[0]
    read = lambda c: row[headers.index(c)].strip() if len(row) > headers.index(c) else ""
    return i, read("Этап"), read(START_COL), read(END_COL), headers.index(START_COL) + 1, headers.index(END_COL) + 1


def missing():
    """Show all stages read-only; other projects' stages are not writable in pilot."""
    _guard()
    ws, _ = _sheets()
    result = []
    for stage in (1, 2, 3):
        _, name, a, b, _, _ = _stage_row(ws, stage)
        result.append({"этап": stage, "название": name,
                       "начало": a or "не назначено",
                       "завершение": b or "не назначено"})
    return result


def draft(stage, first, last):
    if stage not in (1, 2, 3):
        raise ValueError("В тесте доступны только этапы 1–3 ЖК МОД")
    start, end = parse_date(first), parse_date(last)
    if start > end:
        raise ValueError("Начало этапа позже его окончания")
    _guard()
    ws, gantt = _sheets()
    row, name, old_start, old_end, _, _ = _stage_row(ws, stage)
    if old_start == first and old_end == last:
        raise ValueError("Такие даты уже записаны")
    _gantt_row(gantt, stage, name)
    return PlanProposal(project=PROJECT, project_id=PROJECT_ID, pult_id=PULT_ID,
                        stage=stage, stage_name=name, old_start=old_start,
                        old_end=old_end, new_start=first, new_end=last)


def _gantt_row(ws, stage, stage_name):
    rownum = stage + 3  # Source row 2 is Gantt row 4.
    cells = ws.row_values(rownum)
    if (len(cells) < 13 or cells[0] != PROJECT or cells[1] != str(stage)
            or cells[2] != stage_name):
        raise ValueError("Гант не соответствует строке проекта: ручная проверка")
    return rownum


def _gantt_marks(ws, start, end):
    # Use Google Sheets underlying date serials, never infer the year from
    # the displayed dd.MM header (the visible period crosses New Year).
    dates = ws.get("N3:AA3", value_render_option="UNFORMATTED_VALUE")
    if not dates or len(dates[0]) != 14:
        raise ValueError("В Ганте нет полной недельной шкалы")
    origin = datetime.date(1899, 12, 30)
    marks = []
    for value in dates[0]:
        if not isinstance(value, (int, float)):
            raise ValueError("Заголовки Ганта не содержат дат")
        monday = origin + datetime.timedelta(days=int(value))
        sunday = monday + datetime.timedelta(days=6)
        marks.append("■" if monday <= end and sunday >= start else "")
    return marks


def apply(proposal):
    _guard()
    if proposal.project != PROJECT or proposal.project_id != PROJECT_ID or proposal.pult_id != PULT_ID:
        raise PermissionError("В предложении не тестовые таблицы")
    start, end = parse_date(proposal.new_start), parse_date(proposal.new_end)
    if start > end:
        raise ValueError("Неверный период")
    source, gantt = _sheets()
    row, name, old_start, old_end, col_a, col_b = _stage_row(source, proposal.stage)
    if (name != proposal.stage_name or old_start != proposal.old_start
            or old_end != proposal.old_end):
        raise ValueError("Даты изменились после предпросмотра — запись отменена")
    if col_a + 1 != col_b:
        raise ValueError("Столбцы дат изменились, требуется проверка")
    gantt_row = _gantt_row(gantt, proposal.stage, name)
    marks = _gantt_marks(gantt, start, end)
    # Source-of-truth first. Later failures are manual-review cases; update_state
    # single-use claim protects against blindly replaying a partial write.
    source.update(f"C{row}:D{row}", [[proposal.new_start, proposal.new_end]], value_input_option="USER_ENTERED")
    _, _, current_a, current_b, _, _ = _stage_row(source, proposal.stage)
    if (current_a, current_b) != (proposal.new_start, proposal.new_end):
        raise RuntimeError("Даты могли записаться, но чтение из проекта не подтвердило их. Проверь таблицу")
    gantt.update(f"D{gantt_row}:E{gantt_row}", [[proposal.new_start, proposal.new_end]], value_input_option="USER_ENTERED")
    gantt.update(f"N{gantt_row}:AA{gantt_row}", [marks], value_input_option="RAW")
    got = gantt.row_values(gantt_row)
    # Google returns rows trimmed after the last non-empty cell. The 14-week
    # window may end with legitimate empty cells; pad those before comparing.
    padded = (got + [""] * 27)[:27]
    if padded[3:5] != [proposal.new_start, proposal.new_end] or padded[13:27] != marks:
        raise RuntimeError("Тестовый Гант не подтвердил все изменения. Не повторяй запрос; нужна сверка")
    # Journal goes after verified source + derived view; log errors must not be hidden.
    try:
        journal = sheets._gc().open_by_key(PROJECT_ID).worksheet("01 Журнал проекта")
        journal.append_row([
            datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime(D_FMT),
            "Изменение" if proposal.old_start or proposal.old_end else "Планирование",
            f"Тестовое планирование: этап {proposal.stage} «{name}». План: {proposal.old_start or 'не указан'} — {proposal.old_end or 'не указан'} → {proposal.new_start} — {proposal.new_end}.",
            "Я", proposal.new_end, "Telegram: подтверждение дизайнера; внутренний план",
            "Да", "Записано в тестовом проекте и тестовом Ганте; фактические даты и акт не менялись."
        ], value_input_option="USER_ENTERED")
    except Exception as exc:
        raise RuntimeError("Даты проекта и Ганта обновлены, но журнал не подтвердил запись; ручная проверка") from exc
    sheets._cache.clear()
    return f"ЖК МОД, этап {proposal.stage}: {proposal.new_start} — {proposal.new_end}; тестовый Гант обновлён"
