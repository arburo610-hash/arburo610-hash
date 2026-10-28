"""Календарь и напоминания: «образец обоев по МОД 2 ноября, напомни накануне».

Напоминания хранятся в MEDIA_ROOT/напоминания.jsonl и приходят в Telegram (бот проверяет их раз в минуту).
Если задан GOOGLE_CALENDAR_ID и календарь расшарен на сервисный аккаунт, событие ещё и появляется
в Google Календаре.
"""
import datetime
import json
import os
import threading
from zoneinfo import ZoneInfo

import visits

FILE = os.path.join(visits.MEDIA_ROOT, "напоминания.jsonl")
TZ = ZoneInfo("Europe/Moscow")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

_lock = threading.Lock()


def _load():
    if not os.path.exists(FILE):
        return []
    with open(FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _save(items):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in items:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, FILE)


def parse_dt(value):
    """«2026-11-01T10:00» → aware datetime по Москве."""
    return datetime.datetime.fromisoformat(value).replace(tzinfo=TZ)


def add(text, project="", event_date="", event_time="", remind_at="", to_calendar=False):
    """event_date — ГГГГ-ММ-ДД (может быть пусто), remind_at — ГГГГ-ММ-ДДTЧЧ:ММ (может быть пусто — только в календарь)."""
    if remind_at:
        parse_dt(remind_at)  # проверяем формат сразу, чтобы Claude получил понятную ошибку
    if event_date:
        datetime.date.fromisoformat(event_date)
    with _lock:
        items = _load()
        item = {
            "id": max((r["id"] for r in items), default=0) + 1,
            "text": text, "project": project, "event_date": event_date, "event_time": event_time,
            "remind_at": remind_at, "status": "ждёт" if remind_at else "без напоминания", "calendar_event": "",
        }
        items.append(item)
        _save(items)
    if to_calendar and event_date:
        item["calendar_event"] = add_to_calendar(item)
        update(item["id"], calendar_event=item["calendar_event"])
    return item


def update(rid, **fields):
    with _lock:
        items = _load()
        for r in items:
            if r["id"] == rid:
                r.update(fields)
                _save(items)
                return r
    return None


def due(now=None):
    now = now or datetime.datetime.now(TZ)
    return [r for r in _load() if r["status"] == "ждёт" and r["remind_at"] and parse_dt(r["remind_at"]) <= now]


def find(date_from="", date_to="", include_done=False):
    """Напоминания и события, у которых дата события или напоминания попадает в [date_from, date_to]."""
    result = []
    for r in _load():
        if not include_done and r["status"] in ("сделано", "отменено"):
            continue
        dates = [d for d in (r["event_date"], r["remind_at"][:10]) if d]
        if date_from and dates and max(dates) < date_from:
            continue
        if date_to and dates and min(dates) > date_to:
            continue
        result.append(r)
    return result


def human(r):
    when = ""
    if r["event_date"]:
        d = datetime.date.fromisoformat(r["event_date"])
        when = f"{d:%d.%m}" + (f" {r['event_time']}" if r["event_time"] else "")
    project = f" [{r['project']}]" if r["project"] else ""
    return f"#{r['id']}{project} {when + ' — ' if when else ''}{r['text']}"


# ---------- Google Календарь ----------

def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/calendar.events"]
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(
            os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"), scopes=scopes)
    return AuthorizedSession(creds)


def add_to_calendar(r):
    """Создаёт событие в Google Календаре. Возвращает ссылку на событие или текст ошибки."""
    if not CALENDAR_ID:
        return "календарь не подключён"
    title = f"{r['project']}: {r['text']}" if r["project"] else r["text"]
    day = datetime.date.fromisoformat(r["event_date"])
    if r["event_time"]:
        start = datetime.datetime.combine(day, datetime.time.fromisoformat(r["event_time"]), TZ)
        body = {"start": {"dateTime": start.isoformat(), "timeZone": "Europe/Moscow"},
                "end": {"dateTime": (start + datetime.timedelta(hours=1)).isoformat(), "timeZone": "Europe/Moscow"}}
    else:
        body = {"start": {"date": day.isoformat()}, "end": {"date": (day + datetime.timedelta(days=1)).isoformat()}}
    body.update(summary=title, description="Добавлено бизнес-ассистентом")
    try:
        resp = _session().post(f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events", json=body, timeout=30)
    except Exception as e:
        return f"ошибка: {e}"
    if resp.status_code >= 300:
        return f"ошибка {resp.status_code}: {resp.text[:200]}"
    return resp.json().get("htmlLink", "добавлено")
