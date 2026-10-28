"""Зеркало «Входящих» в Google Таблицах: «Заметки | ЖК МОД» в папке каждого проекта, «Заметки | Входящие» — без проекта.

Столбцы: № | Дата | Проект | Фото | Пояснение | Комментарий. Источник правды — заметки.jsonl,
таблица обновляется следом: новая заметка → строка, сменился проект → строка переезжает в другую таблицу.
"""
import logging
import re
import threading
import time

import drive
import photo_server
import sheets

log = logging.getLogger("assistant.notes_sheets")
PREFIX = "Заметки |"
INBOX_KEY = "входящие"
CACHE_SECONDS = 600

_lock = threading.Lock()
_tables = {"at": 0, "map": {}}


def _key(name):
    """«ЖК Талисман | Открытое шоссе 14Д» → «талисман открытое шоссе 14д»."""
    name = re.sub(r"\bжк\b", " ", name.lower())
    return " ".join(re.sub(r"[^\w]+", " ", name).split())


def tables():
    """{ключ проекта: id таблицы} по всем «Заметки | …», доступным роботу."""
    if time.time() - _tables["at"] > CACHE_SECONDS:
        found = drive.search(PREFIX, "таблица")
        _tables["map"] = {_key(f["название"].split("|", 1)[1]): f["id"] for f in found if "|" in f["название"]}
        _tables["at"] = time.time()
    return _tables["map"]


def table_for(project):
    known = tables()
    key = _key(project or "")
    if key:
        for table_key, table_id in known.items():
            if table_key != INBOX_KEY and (table_key in key or key in table_key):
                return table_id
    return known.get(INBOX_KEY)


def _text(value):
    """Текст ячейки, который таблица не примет за формулу."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def row(n):
    image = photo_server.url(n.get("photo_token", ""))
    if image:
        photo = f'=IMAGE("{image}")'  # миниатюра прямо в ячейке
    else:
        photo = _text({"photo": "📷 есть — «покажи #%d»" % n["id"], "video": "🎬 видео — «покажи #%d»" % n["id"],
                       "voice": "🎙 голосовое"}.get(n["kind"], ""))
    explanation = "\n".join(x for x in (n.get("summary", ""), n.get("details", "")) if x)
    comment = "\n".join(x for x in (n.get("text", ""), n.get("caption", ""), n.get("transcript", "")) if x)
    return [n["id"], f"'{n['date']} {n['time']}", _text(n.get("project", "")), photo, _text(explanation), _text(comment)]


def _find_row(ws, note_id):
    for i, value in enumerate(ws.col_values(1), start=1):
        if value.strip() == str(note_id):
            return i
    return None


def sync(note):
    """Кладёт заметку в таблицу её проекта (или во «Входящие»), убирая из прежней. Возвращает id таблицы."""
    with _lock:
        target = table_for(note.get("project", ""))
        if not target:
            log.warning("нет таблицы «%s Входящие» — заметка #%s только в боте", PREFIX, note["id"])
            return None
        old = note.get("table")
        if old and old != target:
            ws_old = sheets._gc().open_by_key(old).sheet1
            r = _find_row(ws_old, note["id"])
            if r:
                ws_old.delete_rows(r)
        ws = sheets._gc().open_by_key(target).sheet1
        r = _find_row(ws, note["id"])
        if r:
            ws.update(range_name=f"A{r}:F{r}", values=[row(note)], value_input_option="USER_ENTERED")
        else:
            ws.append_row(row(note), value_input_option="USER_ENTERED", table_range="A1")
        return target


def sync_later(note, on_done=None):
    """В фоне, чтобы бот не ждал Google. on_done(table_id) — сохранить, в какой таблице теперь заметка."""
    def run():
        try:
            table = sync(note)
            if table and on_done:
                on_done(table)
        except Exception:
            log.exception("не удалось записать заметку #%s в таблицу", note.get("id"))
    threading.Thread(target=run, daemon=True).start()
