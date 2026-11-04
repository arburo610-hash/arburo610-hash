"""Safe, opt-in changes to existing project records.

Design principles:
- no write without user's separate confirmation;
- exact row and expected-cell checks;
- no speculative dates or automatic closure of an act;
- read-back verification; never claim success when Google did not persist changes.
"""
import os
import datetime
from zoneinfo import ZoneInfo
from typing import Literal
from pydantic import BaseModel, Field

import sheets

class Patch(BaseModel):
    section: Literal["этапы", "задачи"]
    record_id: str = Field(description="For stages use their № (1/2/3); for tasks use existing ID")
    column: str = Field(description="Exact column heading from the selected project sheet")
    expected: str = Field(description="Existing value exactly as read, or blank")
    value: str = Field(description="Proposed new value. Never invent dates or signed acts")
    reason: str = Field(description="Why this field should change")

class NewTask(BaseModel):
    title: str = Field(description="Clear, concrete action; no invented commitments")
    stage: Literal["Этап 1", "Этап 2", "Этап 3", "Документы"]
    responsible: Literal["Я", "Клиент", "Визуализатор", "Чертёжник"] = "Я"
    priority: Literal["Высокий", "Средний", "Низкий"] = "Средний"
    dependency: str = ""
    next_step: str = ""
    reason: str = ""


class Proposal(BaseModel):
    project: str
    summary: str
    patches: list[Patch] = Field(default_factory=list, max_length=12)
    unresolved: list[str] = Field(default_factory=list)
    new_tasks: list[NewTask] = Field(default_factory=list, max_length=6)
    source_message: str = ""

ALLOWED_FIELDS = {
    "этапы": {"Блокер / примечание", "Следующий шаг", "Статус выполнения работ", "Статус документооборота"},
    "задачи": {"Статус", "Следующий шаг", "Зависит от / ожидаем"},
}
# Статусы этапов и юридически значимые поля требуют отдельной схемы согласования.


def context_for(project_name):
    project = sheets.find_project(project_name)
    if not project:
        raise ValueError("Не найден проект в Пульте")
    result = {"проект": project.get("Проект"), "пульт": {
        "Статус": project.get("Статус"), "Текущий этап / период": project.get("Текущий этап / период")}}
    for section in ("этапы", "задачи"):
        result[section] = sheets.read_project_section(project_name, section, only_open=False)["записи"][-70:]
    result["журнал"] = sheets.read_project_section(project_name, "журнал", only_open=False)["записи"][-12:]
    return result


def _existing_task_titles(project_name):
    data = sheets.read_project_section(project_name, "задачи", only_open=False)["записи"]
    return {x.get("Задача", "").strip().casefold() for x in data}


def inspect_new_task(project_name, task: NewTask):
    if not task.title.strip():
        raise ValueError("Пустое название задачи")
    if task.title.strip().casefold() in _existing_task_titles(project_name):
        raise ValueError("Задача с таким названием уже существует")
    if len(task.title) > 180:
        raise ValueError("Название задачи слишком длинное")
    if task.reason and not task.reason.strip():
        raise ValueError("Не указано основание задачи")


def inspect_patch(project_name, patch: Patch):
    if patch.column not in ALLOWED_FIELDS[patch.section]:
        raise ValueError(f"Запрещено изменять поле {patch.section}: {patch.column}")
    _project, _sheet_id, ws = sheets._project_sheet(project_name, patch.section)
    grid = ws.get_all_values()
    head_idx = next((i for i, row in enumerate(grid) if patch.column in row), None)
    if head_idx is None:
        raise ValueError(f"Нет столбца «{patch.column}»")
    header = [x.strip() for x in grid[head_idx]]
    key = "№" if patch.section == "этапы" else "ID"
    if key not in header:
        raise ValueError(f"Нет ключевого столбца {key}")
    found = [(j, row) for j, row in enumerate(grid[head_idx + 1:], start=head_idx + 1)
             if len(row) > header.index(key) and row[header.index(key)].strip() == patch.record_id]
    if len(found) != 1:
        raise ValueError(f"Не найден единственный ID {patch.record_id} в {patch.section}")
    index, row = found[0]
    column = header.index(patch.column)
    actual = row[column] if len(row) > column else ""
    if actual != patch.expected:
        raise ValueError(f"Запись {patch.record_id} изменилась: поле «{patch.column}» сейчас «{actual}»")
    if not patch.value.strip() or patch.value == actual:
        raise ValueError("Новое значение пустое либо совпадает со старым")
    return ws, index + 1, column + 1


def prepare(proposal: Proposal, source_message: str = ""):
    """Keep independently safe edits; turn forbidden/conflicting edits into review items.

    Called when drafting, never writes to Google Sheets.
    """
    accepted = []
    rejected = []
    duplicate = set()
    for patch in proposal.patches:
        key = (patch.section, patch.record_id, patch.column)
        if key in duplicate:
            rejected.append(f"{patch.section} #{patch.record_id}: повторная правка «{patch.column}» — проверить вручную")
            continue
        duplicate.add(key)
        # A general report that a stage is finished does NOT prove every task in
        # that stage is complete. For the pilot require a precise task ID before
        # proposing a transition to a final task status.
        if (patch.section == "задачи" and patch.column == "Статус"
                and patch.value.strip().casefold() in {"готово", "выполнено", "закрыто", "закрыта"}
                and patch.record_id.casefold() not in source_message.casefold()):
            rejected.append(
                f"задачи #{patch.record_id}: закрытие требует отдельного прямого "
                "подтверждения именно этой задачи; завершение этапа само по себе недостаточно"
            )
            continue
        if patch.column not in ALLOWED_FIELDS[patch.section]:
            rejected.append(
                f"{patch.section} #{patch.record_id}: изменение «{patch.column}» "
                f"с «{patch.expected}» на «{patch.value}» требует отдельного согласования; "
                "автоматическая запись запрещена"
            )
            continue
        try:
            inspect_patch(proposal.project, patch)
            accepted.append(patch)
        except (ValueError, KeyError) as exc:
            rejected.append(
                f"{patch.section} #{patch.record_id}, «{patch.column}»: "
                f"не подготовлено к записи ({exc})"
            )
    proposal.patches = accepted
    unique_tasks = []
    existing = _existing_task_titles(proposal.project)
    seen = set()
    for task in proposal.new_tasks:
        title = task.title.strip().casefold()
        if not title or title in existing or title in seen:
            rejected.append(f"Новая задача «{task.title}»: пустое имя или дублирование; запись пропущена")
            continue
        try:
            inspect_new_task(proposal.project, task)
        except ValueError as exc:
            rejected.append(f"Новая задача «{task.title}»: {exc}")
            continue
        seen.add(title)
        unique_tasks.append(task)
    proposal.new_tasks = unique_tasks
    proposal.unresolved.extend(rejected)
    return proposal


def validate(proposal: Proposal):
    if not proposal.patches and not proposal.new_tasks:
        raise ValueError("Нет конкретных изменений для записи")
    for patch in proposal.patches:
        inspect_patch(proposal.project, patch)
    for task in proposal.new_tasks:
        inspect_new_task(proposal.project, task)
    return True


def apply(proposal: Proposal):
    # Same test-copy protection for updates and existing append-based workflows.
    project = sheets.find_project(proposal.project)
    if not project:
        raise ValueError("Проект не найден в Пульте")
    spreadsheet_id = sheets._test_override_id(project)
    if not spreadsheet_id:
        raise PermissionError("Для проекта не указана тестовая копия")
    sheets._assert_test_write_allowed(spreadsheet_id)
    # No blind bulk replacement. Stop on conflict and report what was already written.
    validate(proposal)
    changed = []
    log_items = []
    for patch in proposal.patches:
        try:
            ws, row, col = inspect_patch(proposal.project, patch)
            ws.update_cell(row, col, patch.value)
            observed = ws.cell(row, col).value or ""
            if observed != patch.value:
                raise RuntimeError("Google Sheets не подтвердил запись")
            changed.append(f"{patch.section} {patch.record_id}: {patch.column}")
            log_items.append(f"{patch.section} #{patch.record_id}, {patch.column}: «{patch.expected}» → «{patch.value}»")
            sheets._cache.clear()
        except Exception as exc:
            raise RuntimeError(
                "Частичное обновление: " + (", ".join(changed) or "ничего") +
                ". Ошибка: " + str(exc) + ". Не повторяй автоматически."
            ) from exc
    for task in proposal.new_tasks:
        try:
            inspect_new_task(proposal.project, task)
            ids = sheets.append_records(proposal.project, "задачи", [{
                "Этап": task.stage, "Задача": task.title, "Ответственный": task.responsible,
                "Приоритет": task.priority, "Статус": "Не начата",
                "Зависит от / ожидаем": task.dependency,
                "Следующий шаг": task.next_step,
                "Ссылка / примечание": "Источник: сообщение дизайнера; " + task.reason
            }], id_letter="T")
            label = ids[0] if ids else task.title
            changed.append(f"Новая задача {label}")
            log_items.append(f"Добавлена задача {label}: {task.title}")
        except Exception as exc:
            raise RuntimeError(
                "Частичное обновление: " + (", ".join(changed) or "ничего") +
                ". Ошибка новой задачи: " + str(exc) + ". Не повторяй автоматически."
            ) from exc
    if log_items:
        try:
            sheets.append_records(proposal.project, "журнал", [{
                "Дата": datetime.datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y"),
                "Тип": "Изменение", "Содержание": "; ".join(log_items)[:3500],
                "Ответственный / участник": "Я / GPT-ассистент",
                "Источник": "Подтверждённое сообщение в тестовом Telegram",
                "Комментарий": ("Исходное сообщение: " + proposal.source_message)[:2000],
            }])
        except Exception as exc:
            raise RuntimeError(
                "Изменения сохранены (" + ", ".join(changed) +
                "), НО запись в журнал не удалась: " + str(exc) +
                ". Не повторяй сохранение автоматически."
            ) from exc
    return changed
