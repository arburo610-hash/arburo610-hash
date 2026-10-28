"""Чтение рабочей среды из Google Sheets: «00 ПУЛЬТ УПРАВЛЕНИЯ ПРОЕКТАМИ» и таблицы «Управление проектом | …».

Доступ — через сервисный аккаунт Google. Таблицы нужно расшарить на его e-mail (права «Читатель»).
"""
import os
import re
import time

import gspread

PULT_ID = os.environ.get("PULT_SPREADSHEET_ID", "1U36B8N-5Su94kX6FSMq4Xmxw7dOngygdSd9Dpi73yjk")
PULT_SHEET = "Проекты"

# Разделы таблицы проекта: короткое имя -> префикс названия листа
SECTIONS = {
    "панель": "00 Панель",
    "паспорт": "01 Паспорт",
    "журнал": "01 Журнал",
    "этапы": "02 Этапы",
    "задачи": "03 Задачи",
    "решения": "04 Решения",
    "вопросы": "05 Открытые вопросы",
    "визуализатор": "06 Визуализатор",
    "чертёжник": "07 Чертёжник",
}

# Статусы, которые считаем закрытыми при only_open=True
CLOSED_STATUSES = {"готово", "решён", "решен", "закрыт", "закрыта", "неактуально", "отменено", "выполнено", "заменено", "устарело"}

CACHE_SECONDS = 120

_client = None
_cache = {}


def _gc():
    global _client
    if _client is None:
        path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        _client = gspread.service_account(filename=path)
    return _client


def _cached(key, loader):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = loader()
    _cache[key] = (time.time(), value)
    return value


def _rows_as_dicts(values):
    """Первая непустая строка с ≥3 заполненными ячейками — заголовок, остальное — записи."""
    header_idx = next((i for i, row in enumerate(values) if sum(1 for c in row if c.strip()) >= 3), None)
    if header_idx is None:
        # Лист-анкета вида «Параметр | Значение» (паспорт, панель) — отдаём как пары
        return [{"поле": row[0], "значение": " | ".join(c for c in row[1:] if c)} for row in values if row and row[0]]
    header = [h.strip() for h in values[header_idx]]
    records = []
    for row in values[header_idx + 1:]:
        if not any(c.strip() for c in row):
            continue
        records.append({h: v for h, v in zip(header, row) if h and v.strip()})
    return records


def list_projects():
    """Строки «Пульта»: проект, статус, этап, ближайший срок, счётчики, ссылка на таблицу управления."""
    def load():
        ws = _gc().open_by_key(PULT_ID).worksheet(PULT_SHEET)
        return _rows_as_dicts(ws.get_all_values())
    return _cached("pult", load)


def _spreadsheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url or "")
    return m.group(1) if m else None


def find_project(name):
    """Ищет проект в «Пульте» по части названия (без учёта регистра)."""
    needle = name.lower().replace("жк", "").strip()
    for p in list_projects():
        if needle and needle in p.get("Проект", "").lower():
            return p
    return None


def read_project_section(project_name, section, only_open=True):
    project = find_project(project_name)
    if project is None:
        names = ", ".join(p.get("Проект", "") for p in list_projects())
        raise ValueError(f"Проект «{project_name}» не найден в Пульте. Есть: {names}")
    sheet_id = _spreadsheet_id(project.get("Таблица управления"))
    if not sheet_id:
        raise ValueError(f"У проекта «{project['Проект']}» в Пульте нет ссылки на таблицу управления")
    prefix = SECTIONS[section]

    def load():
        book = _gc().open_by_key(sheet_id)
        ws = next((w for w in book.worksheets() if w.title.startswith(prefix)), None)
        if ws is None:
            raise ValueError(f"В таблице проекта нет листа «{prefix}…»")
        return _rows_as_dicts(ws.get_all_values())

    records = _cached((sheet_id, section), load)
    if only_open:
        records = [r for r in records
                   if r.get("Статус", r.get("Статус записи", "")).strip().lower() not in CLOSED_STATUSES]
    return {"проект": project["Проект"], "раздел": section, "записей": len(records), "записи": records}
