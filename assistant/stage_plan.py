"""Pilot stage scheduling for the *test* project ЖК ОСТРОВ only.

Never write working project sheets. All updates are individually approved in
Telegram and journaled in a separate test-only history sheet. The project
stage dates are authoritative; the test Gantt is a derived view.
"""
import datetime
import os
import re

from pydantic import BaseModel
import sheets


PROJECT_ID = "1aHDxoWqo5jVpSIlPb1KsEwjGfnvH-x2ceUI_e6ftflU"
WORKING_PROJECT_ID = "1Uey8MstJ_jbJBriHRTKg5dtnS6pqzvvqSWQYS1oq6C4"
PULT_ID = "1a5ZQWEG6EB576NO8XYxXZ2zMTvYY8p4XYSbctQayF6M"
PROJECT = "ЖК ОСТРОВ"
GANTT = "Гант | ТЕСТ"
STAGES = "02 Этапы"
HISTORY = "История сроков | ТЕСТ"
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


def parse_date(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
        raise ValueError("Нужна точная дата ДД.ММ.ГГГГ. Не допускаются приблизительные сроки")
    try:
        return datetime.datetime.strptime(text, D_FMT).date()
    except ValueError as exc:
        raise ValueError("Это несуществующая календарная дата") from exc


def _guard():
    """Hard ID checks and test flags before any project or Gantt write."""
    if os.environ.get("TEST_PULT_SPREADSHEET_ID", "").strip() != PULT_ID:
        raise PermissionError("Нужна подтверждённая копия тестового Пульта")
    if sheets.PULT_ID == PULT_ID or sheets._pult_read_id() != PULT_ID:
        raise PermissionError("Нельзя использовать рабочий Пульт вместо тестового")
    if os.environ.get("PROJECT_UPDATES_ENABLED", "").lower() != "true":
        raise PermissionError("Тестовая запись плановых дат выключена")
    if os.environ.get("PULT_SYNC_ENABLED", "").lower() != "true":
        raise PermissionError("Тестовая синхронизация Ганта выключена")
    if PROJECT_ID == WORKING_PROJECT_ID:
        raise PermissionError("Запрещена запись в рабочую таблицу проекта")
    project = sheets.find_project(PROJECT)
    if not project or project.get("Проект") != PROJECT:
        raise ValueError("ЖК ОСТРОВ отсутствует в тестовом Пульте")
    linked_id = sheets._spreadsheet_id(project.get("Таблица управления", ""))
    if linked_id != PROJECT_ID:
        raise PermissionError("Ссылка на ЖК ОСТРОВ в тестовом Пульте не ведёт на проверенную тестовую копию")


def _sheets():
    client = sheets._gc()
    return client.open_by_key(PROJECT_ID).worksheet(STAGES), client.open_by_key(PULT_ID).worksheet(GANTT)


def _stage_row(ws, stage):
    grid = ws.get_all_values()
    if not grid or not {"№", "Этап", START_COL, END_COL}.issubset(set(grid[0])):
        raise ValueError("Изменились заголовки листа «02 Этапы»")
    headers = grid[0]
    col_num = headers.index("№")
    matches = [(i + 1, row) for i, row in enumerate(grid[1:], start=1)
               if len(row) > col_num and row[col_num].strip() == str(stage)]
    if len(matches) != 1:
        raise ValueError("Не найден уникальный номер этапа")
    i, row = matches[0]

    def val(column):
        j = headers.index(column)
        return row[j].strip() if len(row) > j else ""

    return i, val("Этап"), val(START_COL), val(END_COL), headers.index(START_COL) + 1, headers.index(END_COL) + 1


def _gantt_row(ws, stage, stage_name):
    index = stage + 9  # ЖК ОСТРОВ occupies rows 10, 11, 12.
    items = ws.row_values(index)
    if len(items) < 13 or items[0] != PROJECT or items[1] != str(stage) or items[2] != stage_name:
        raise ValueError("Строка ЖК ОСТРОВ в Ганте изменилась: требуется ручная проверка")
    return index, items


def _gantt_marks(ws, start, end):
    dates = ws.get("N3:AA3", value_render_option="UNFORMATTED_VALUE")
    if not dates or len(dates[0]) != 14:
        raise ValueError("Нет полной недельной шкалы тестового Ганта")
    base = datetime.date(1899, 12, 30)
    weeks = []
    marks = []
    for serial in dates[0]:
        if not isinstance(serial, (float, int)):
            raise ValueError("Недельная шкала Ганта содержит не дату")
        monday = base + datetime.timedelta(days=int(serial))
        weeks.append(monday)
        sunday = monday + datetime.timedelta(days=6)
        marks.append("■" if monday <= end and sunday >= start else "")
    if start < weeks[0] or end > weeks[-1] + datetime.timedelta(days=6):
        raise ValueError(
            "Предложенный период выходит за видимую шкалу Ганта. "
            "Сначала необходимо расширить график; в исходный проект ничего не записано"
        )
    return marks


def missing():
    _guard()
    source, _ = _sheets()
    out = []
    for stage in (1, 2, 3):
        _, title, a, b, _, _ = _stage_row(source, stage)
        out.append({"этап": stage, "название": title,
                    "начало": a or "не указано", "завершение": b or "не указано"})
    return out


def draft(stage, first, last):
    if stage not in (1, 2, 3):
        raise ValueError("В пилоте доступны только этапы 1–3 ЖК ОСТРОВ")
    start, end = parse_date(first), parse_date(last)
    if start > end:
        raise ValueError("Дата начала позже даты завершения")
    _guard()
    source, gantt = _sheets()
    _, name, old_start, old_end, _, _ = _stage_row(source, stage)
    if first == old_start and last == old_end:
        raise ValueError("Предложенные даты уже записаны")
    row, recorded = _gantt_row(gantt, stage, name)
    if (recorded[3] if len(recorded) > 3 else "") != old_start or (
        recorded[4] if len(recorded) > 4 else "") != old_end:
        raise ValueError("Гант и исходный проект расходятся по прежним датам. Нужна проверка")
    _gantt_marks(gantt, start, end)
    return PlanProposal(project=PROJECT, project_id=PROJECT_ID, pult_id=PULT_ID,
                        stage=stage, stage_name=name, old_start=old_start, old_end=old_end,
                        new_start=first, new_end=last)


def apply(proposal):
    _guard()
    if (proposal.project != PROJECT or proposal.project_id != PROJECT_ID
            or proposal.pult_id != PULT_ID or proposal.stage not in (1, 2, 3)):
        raise PermissionError("Предложение не относится к тестовому ЖК ОСТРОВ")
    first, last = parse_date(proposal.new_start), parse_date(proposal.new_end)
    if first > last:
        raise ValueError("Дата начала позже окончания")
    source, gantt = _sheets()
    row, name, old_start, old_end, col_a, col_b = _stage_row(source, proposal.stage)
    if ((name, old_start, old_end) != (proposal.stage_name, proposal.old_start, proposal.old_end)
            or (col_a, col_b) != (3, 4)):
        raise ValueError("Данные этапа изменились после предпросмотра. Ничего не записано")
    gantt_row, recorded = _gantt_row(gantt, proposal.stage, name)
    if (recorded[3] if len(recorded) > 3 else "") != old_start or (
        recorded[4] if len(recorded) > 4 else "") != old_end:
        raise ValueError("Гант изменился после предпросмотра. Ничего не записано")
    marks = _gantt_marks(gantt, first, last)

    # Write authoritative stage dates first. Following failures mean
    # manual reconciliation; Telegram update_state prevents repeat apply.
    source.batch_update(
        [{"range": f"C{row}:D{row}", "values": [[proposal.new_start, proposal.new_end]]}],
        value_input_option="USER_ENTERED"
    )
    _, _, verified_a, verified_b, _, _ = _stage_row(source, proposal.stage)
    if (verified_a, verified_b) != (proposal.new_start, proposal.new_end):
        raise RuntimeError("Запись дат могла выполниться, но проверка проекта не прошла")

    gantt.batch_update([
        {"range": f"D{gantt_row}:E{gantt_row}", "values": [[proposal.new_start, proposal.new_end]]},
        {"range": f"N{gantt_row}:AA{gantt_row}", "values": [marks]},
    ], value_input_option="USER_ENTERED")
    view = (gantt.row_values(gantt_row) + [""] * 27)[:27]
    if view[3:5] != [proposal.new_start, proposal.new_end] or view[13:27] != marks:
        raise RuntimeError("Изменённый тестовый Гант требует проверки. Не подтверждать повторно")

    journal = sheets._gc().open_by_key(PROJECT_ID).worksheet(HISTORY)
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    journal.append_row(
        [stamp, proposal.stage, name, old_start, old_end,
         proposal.new_start, proposal.new_end,
         "Изменение внутреннего плана" if old_start or old_end else "Новый внутренний план",
         "Подтверждено пользователем в тестовом Telegram. Не означает подписания соглашения, аванса, акта или фактического начала."],
        value_input_option="RAW"
    )
    sheets._cache.clear()
    return (f"ЖК ОСТРОВ: этап {proposal.stage}, план {proposal.new_start} — "
            f"{proposal.new_end}; тестовый Гант и история обновлены")
