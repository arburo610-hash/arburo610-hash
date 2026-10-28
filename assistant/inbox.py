"""«Входящие»: быстрые заметки без выезда — артикул, фото образца, голосовая мысль.

Всё складывается в MEDIA_ROOT/Входящие/: файлы + заметки.jsonl (по строке на заметку).
Разбирать можно потом: спросить ассистента, отнести к проекту, собрать отчёт командой /разобрать.
"""
import datetime
import json
import os
import shutil
import threading
from typing import List
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

import visits

INBOX_DIR = os.path.join(visits.MEDIA_ROOT, "Входящие")
NOTES_FILE = os.path.join(INBOX_DIR, "заметки.jsonl")
TZ = ZoneInfo("Europe/Moscow")
NEW, DONE = "новая", "разобрана"

_lock = threading.Lock()


class MediaNote(BaseModel):
    """Что Claude понял из фото / видео / голосового, присланного вне выезда."""
    is_request: bool = Field(description="true, если это вопрос или поручение ассистенту («что по МОД?», «составь отчёт»), а не информация, которую надо запомнить")
    summary: str = Field(description="Коротко, 1 предложение: что это. «Обои Arte, артикул 12345, бежевый лён»")
    details: str = Field(description="Всё конкретное, что видно или сказано: артикулы, бренды, коллекции, размеры, цены, контакты, текст с этикеток и визиток. Пусто, если нет")
    project: str = Field(description="Проект из списка, к которому это относится, если названо или очевидно; иначе пусто")
    tags: List[str] = Field(description="1–3 метки: обои, плитка, свет, мебель, сантехника, контакт, идея, замер…")


def _load():
    if not os.path.exists(NOTES_FILE):
        return []
    with open(NOTES_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _save(notes):
    os.makedirs(INBOX_DIR, exist_ok=True)
    tmp = NOTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for n in notes:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    os.replace(tmp, NOTES_FILE)


def media_path(kind, ext):
    os.makedirs(INBOX_DIR, exist_ok=True)
    names = {"photo": "фото", "video": "видео", "voice": "голосовое"}
    stamp = datetime.datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(INBOX_DIR, f"{stamp}_{names.get(kind, kind)}{ext}")


def add(kind, text="", path="", caption="", transcript="", frames=None, summary="", details="", project="", tags=None):
    now = datetime.datetime.now(TZ)
    with _lock:
        notes = _load()
        note = {
            "id": max((n["id"] for n in notes), default=0) + 1,
            "date": now.strftime("%d.%m.%Y"), "time": now.strftime("%H:%M"),
            "kind": kind, "text": text, "caption": caption, "transcript": transcript,
            "summary": summary, "details": details, "project": project, "tags": tags or [],
            "path": path, "frames": frames or [], "status": NEW,
        }
        notes.append(note)
        _save(notes)
    return note


def find(project="", status=NEW, query="", ids=None):
    """status: «новая», «разобрана» или пусто — все. project и query — подстрока без учёта регистра."""
    result = []
    for n in _load():
        if ids and n["id"] not in ids:
            continue
        if status and n["status"] != status:
            continue
        if project and project.lower().replace("жк", "").strip() not in n["project"].lower():
            continue
        if query:
            haystack = " ".join([n["text"], n["caption"], n["transcript"], n["summary"], n["details"], " ".join(n["tags"])]).lower()
            if query.lower() not in haystack:
                continue
        result.append(n)
    return result


def update(ids, **fields):
    with _lock:
        notes = _load()
        changed = []
        for n in notes:
            if n["id"] in ids:
                n.update({k: v for k, v in fields.items() if v is not None})
                changed.append(n["id"])
        _save(notes)
    return changed


def short(n):
    """Строка для списка в Telegram и для Claude."""
    what = n["summary"] or n["text"] or n["transcript"] or n["caption"] or "(без описания)"
    icon = {"photo": "📷", "video": "🎬", "voice": "🎙", "text": "📝"}.get(n["kind"], "•")
    project = f" [{n['project']}]" if n["project"] else ""
    return f"#{n['id']} {icon} {n['date']}{project} — {what[:200]}"


def for_claude(n):
    keys = ("id", "date", "time", "kind", "project", "summary", "details", "text", "caption", "transcript", "tags", "status")
    return {k: n[k] for k in keys if n.get(k)}


def to_visit(notes, project):
    """Собирает заметки в «выезд», чтобы прогнать через тот же отчёт. Файлы копируются, входящие не трогаем."""
    visit, _ = visits.start_visit(project, "разбор входящих")
    for n in notes:
        item = visits.Item(kind=n["kind"], time=f"{n['date']} {n['time']}",
                           caption=" ".join(x for x in (n["text"], n["caption"], n["details"]) if x),
                           transcript=n["transcript"])
        if n["path"] and os.path.exists(n["path"]):
            item.path = os.path.join(visit.folder, os.path.basename(n["path"]))
            shutil.copy2(n["path"], item.path)
        elif item.kind == "photo":  # файла нет — передаём хотя бы описание
            item.kind, item.caption = "text", f"(фото не сохранилось) {n['summary']} {item.caption}"
        item.frames = [f for f in n.get("frames", []) if os.path.exists(f)]
        visit.items.append(item)
    return visit
