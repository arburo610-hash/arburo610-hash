"""Чтение рабочей среды из Google Sheets: «00 ПУЛЬТ УПРАВЛЕНИЯ ПРОЕКТАМИ» и таблицы «Управление проектом | …».

Доступ — через сервисный аккаунт Google. Таблицы нужно расшарить на его e-mail (права «Читатель»).
"""
import json
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
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # содержимое ключа целиком — удобно для Railway
        if raw:
            _client = gspread.service_account_from_dict(json.loads(raw))
        else:
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


def _project_sheet(project_name, section):
    """(строка проекта из Пульта, id таблицы проекта, лист раздела)."""
    project = find_project(project_name)
    if project is None:
        names = ", ".join(p.get("Проект", "") for p in list_projects())
        raise ValueError(f"Проект «{project_name}» не найден в Пульте. Есть: {names}")
    sheet_id = _spreadsheet_id(project.get("Таблица управления"))
    if not sheet_id:
        raise ValueError(f"У проекта «{project['Проект']}» в Пульте нет ссылки на таблицу управления")
    prefix = SECTIONS[section]
    ws = next((w for w in _gc().open_by_key(sheet_id).worksheets() if w.title.startswith(prefix)), None)
    if ws is None:
        raise ValueError(f"В таблице проекта нет листа «{prefix}…»")
    return project, sheet_id, ws


def read_project_section(project_name, section, only_open=True):
    project = find_project(project_name)
    key = (project and project.get("Таблица управления"), section)
    project, _, ws = _cached(("sheet",) + key, lambda: _project_sheet(project_name, section))
    records = _cached(key, lambda: _rows_as_dicts(ws.get_all_values()))
    if only_open:
        records = [r for r in records
                   if r.get("Статус", r.get("Статус записи", "")).strip().lower() not in CLOSED_STATUSES]
    return {"проект": project["Проект"], "раздел": section, "записей": len(records), "записи": records}


def append_records(project_name, section, records, id_letter=None):
    """Дописывает записи в конец раздела, раскладывая поля по заголовкам листа.

    id_letter — буква в ID раздела (T — задачи, Q — вопросы): следующий номер берётся по максимальному
    существующему, префикс — как у остальных записей (MOD-T-021). Возвращает присвоенные ID.
    Нужен доступ сервисного аккаунта на редактирование таблицы.
    """
    project, sheet_id, ws = _project_sheet(project_name, section)
    values = ws.get_all_values()
    header_idx = next(i for i, row in enumerate(values) if sum(1 for c in row if c.strip()) >= 3)
    header = [h.strip() for h in values[header_idx]]

    ids = []
    if id_letter:
        pattern = re.compile(rf"^(.*-{id_letter}-)(\d+)$")
        found = [m for row in values[header_idx + 1:] if row and (m := pattern.match(row[0].strip()))]
        prefix = found[0].group(1) if found else f"{id_letter}-"
        width = len(found[0].group(2)) if found else 3
        start = max((int(m.group(2)) for m in found), default=0) + 1
        ids = [f"{prefix}{n:0{width}d}" for n in range(start, start + len(records))]
        records = [{**r, "ID": i} for r, i in zip(records, ids)]

    rows = [[r.get(h, "") for h in header] for r in records]
    ws.append_rows(rows, value_input_option="USER_ENTERED", table_range=f"A{header_idx + 1}")
    _cache.clear()
    return ids
