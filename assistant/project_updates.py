"""Safe, opt-in changes to existing project records.

Design principles:
- no write without user's separate confirmation;
- exact row and expected-cell checks;
- no speculative dates or automatic closure of an act;
- read-back verification; never claim success when Google did not persist changes.
"""
import os
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

class Proposal(BaseModel):
    project: str
    summary: str
    patches: list[Patch] = Field(default_factory=list, max_length=12)
    unresolved: list[str] = Field(default_factory=list)

ALLOWED_FIELDS = {
    "этапы": {"Блокер / примечание", "Следующий шаг"},
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
    return result


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


def validate(proposal: Proposal):
    if not proposal.patches:
        raise ValueError("Нет конкретных изменений для записи")
    for patch in proposal.patches:
        inspect_patch(proposal.project, patch)
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
    changed = []
    for patch in proposal.patches:
        try:
            ws, row, col = inspect_patch(proposal.project, patch)
            ws.update_cell(row, col, patch.value)
            # Read back the exact cell before reporting success.
            observed = ws.cell(row, col).value or ""
            if observed != patch.value:
                raise RuntimeError("Google Sheets не подтвердил запись")
            changed.append(f"{patch.section} {patch.record_id}: {patch.column}")
            sheets._cache.clear()
        except Exception as e:
            raise RuntimeError(
                "Обновление остановлено. Успели записать: " + (", ".join(changed) or "ничего") +
                ". Ошибка: " + str(e)
            ) from e
    return changed
