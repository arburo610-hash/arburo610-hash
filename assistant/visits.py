"""Режим «выезда»: собираем фото, видео, голосовые и заметки с объекта или от поставщика,
потом одним запросом к Claude получаем отчёт заказчику, протокол, подписи к фото и строки для таблицы проекта.

Все материалы сразу сохраняются в папку
    MEDIA_ROOT/Выезды/<Проект>/<ГГГГ-ММ-ДД> <место>/
Если MEDIA_ROOT лежит внутри Google Диска для компьютера (G:\\Мой диск\\…), папка сама синхронизируется в Drive.
"""
import datetime
import os
import re
from dataclasses import dataclass, field
from typing import List, Literal

from pydantic import BaseModel, Field

import media
import sheets

MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "media")
MAX_IMAGES = 40  # сколько картинок (фото + кадры видео) отправляем в Claude за один выезд


@dataclass
class Item:
    kind: str  # photo | video | voice | text
    path: str = ""
    caption: str = ""
    transcript: str = ""
    frames: List[str] = field(default_factory=list)
    time: str = ""


@dataclass
class Visit:
    project: str
    place: str
    folder: str
    date: str
    items: List[Item] = field(default_factory=list)

    def count(self, kind):
        return sum(1 for i in self.items if i.kind == kind)


def safe_name(text, limit=60):
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text).strip()
    return re.sub(r"\s+", " ", text)[:limit].strip() or "без названия"


def start_visit(project_name, place):
    project = sheets.find_project(project_name)
    project_title = project["Проект"] if project else project_name
    today = datetime.date.today()
    folder = os.path.join(MEDIA_ROOT, "Выезды", safe_name(project_title), f"{today:%Y-%m-%d} {safe_name(place)}")
    os.makedirs(folder, exist_ok=True)
    return Visit(project=project_title, place=place, folder=folder, date=f"{today:%d.%m.%Y}"), project is not None


# ---------- Что Claude возвращает по итогам выезда ----------

class PhotoNote(BaseModel):
    number: int = Field(description="Номер фото из списка материалов")
    title: str = Field(description="Короткое название для имени файла, 3–6 слов: «образец фасада дуб натуральный»")
    description: str = Field(description="Что на фото и к какому вопросу/решению относится — так, чтобы через месяц было понятно")


class ClientQuestion(BaseModel):
    question: str = Field(description="Вопрос заказчику коротко")
    details: str = Field(description="Что нужно решить, варианты, последствия")
    owner: str = Field(description="Кто отвечает: Клиент / Я / подрядчик")
    priority: Literal["Высокий", "Средний", "Низкий"]
    blocks_stage: Literal["Да", "Нет"]
    affects: str = Field(description="На что влияет")


class TaskNote(BaseModel):
    task: str
    owner: str = Field(description="Я / Клиент / название подрядчика")
    due: str = Field(description="Срок ДД.ММ.ГГГГ, только если прозвучал явно, иначе пусто")
    priority: Literal["Высокий", "Средний", "Низкий"]
    next_step: str


class EventNote(BaseModel):
    kind: Literal["Событие", "Изменение", "Решение"]
    content: str
    participants: str


class VisitResult(BaseModel):
    client_report: str = Field(description="Отчёт для заказчика: готовый текст для мессенджера от лица Анастасии")
    internal_summary: str = Field(description="Короткий протокол выезда для самой Анастасии")
    photos: List[PhotoNote]
    client_questions: List[ClientQuestion] = Field(description="Вопросы, которые нужно согласовать с заказчиком")
    tasks: List[TaskNote] = Field(description="Задачи по итогам выезда")
    events: List[EventNote] = Field(description="Что произошло и что решили на месте — для журнала проекта")
    unclear: List[str] = Field(description="Что непонятно из материалов — что уточнить у Анастасии")


VISIT_PROMPT = """Ниже материалы с выезда в хронологическом порядке: фото, кадры из видео, расшифровки голосовых и видео, заметки.
Проект: {project}. Место: {place}. Дата: {date}.

Открытые вопросы и актуальные решения проекта (чтобы не дублировать и ссылаться на ID):
{context}

Сделай:
1. client_report — отчёт для заказчика, который Анастасия отправит в Telegram или MAX. Спокойный, уважительный, по-человечески, от первого лица. Структура: что сделали и увидели на выезде → что обсудили с рабочими или поставщиком → что нужно согласовать (нумерованный список, по каждому пункту варианты и до какого числа нужен ответ, если срок прозвучал). Без канцелярита, без внутренних ID. Видео упоминай как «видеообзор, который я отправила».
2. internal_summary — протокол для Анастасии: кто был, что решили, что осталось открытым. Можно со ссылками на ID из таблицы.
3. photos — подпись к КАЖДОМУ фото по номеру: что на нём (образец, узел, проблема), какой поставщик/помещение, к какому вопросу относится.
4. client_questions, tasks, events — строки для таблицы проекта. Не дублируй уже открытые вопросы; если новый вопрос продолжает старый — укажи старый ID в тексте.
5. unclear — что не удалось понять (неразборчиво, нет контекста, непонятно какой образец что значит).
Только то, что есть в материалах. Не придумывай размеры, цены, сроки и решения.
{extra}"""


def _project_context(project):
    parts = []
    for section in ("вопросы", "решения"):
        try:
            data = sheets.read_project_section(project, section)
        except Exception as e:  # таблица недоступна — работаем без контекста
            parts.append(f"({section}: не удалось прочитать — {e})")
            continue
        for r in data["записи"][-30:]:
            parts.append(f"{r.get('ID', '')}: {r.get('Вопрос') or r.get('Тема', '')} — "
                         f"{(r.get('Что нужно решить') or r.get('Решение / изменение') or '')[:200]}")
    return "\n".join(parts) or "(нет данных)"


def build_content(visit, extra_instruction=""):
    content = [{"type": "text", "text": VISIT_PROMPT.format(
        project=visit.project, place=visit.place, date=visit.date,
        context=_project_context(visit.project),
        extra=f"\nДополнительно от Анастасии: {extra_instruction}" if extra_instruction else "",
    )}]
    images_left = MAX_IMAGES
    photo_no = video_no = voice_no = 0
    for item in visit.items:
        if item.kind == "photo":
            photo_no += 1
            label = f"[Фото {photo_no}, {item.time}]" + (f" Подпись: {item.caption}" if item.caption else "")
            content.append({"type": "text", "text": label})
            if images_left > 0:
                content.append(media.image_block(item.path))
                images_left -= 1
        elif item.kind == "video":
            video_no += 1
            text = f"[Видео {video_no}, {item.time}]"
            if item.caption:
                text += f" Подпись: {item.caption}"
            text += f"\nРасшифровка: {item.transcript or '(нет расшифровки)'}"
            content.append({"type": "text", "text": text})
            for frame in item.frames:
                if images_left > 0:
                    content.append(media.image_block(frame))
                    images_left -= 1
        elif item.kind == "voice":
            voice_no += 1
            content.append({"type": "text", "text": f"[Голосовое {voice_no}, {item.time}] {item.transcript or '(не расшифровано)'}"})
        else:
            content.append({"type": "text", "text": f"[Заметка, {item.time}] {item.caption}"})
    return content


def apply_photo_names(visit, result):
    """Переименовывает фото по подписям Claude: «03 образец фасада дуб.jpg»."""
    photos = [i for i in visit.items if i.kind == "photo"]
    for note in result.photos:
        if 1 <= note.number <= len(photos):
            item = photos[note.number - 1]
            ext = os.path.splitext(item.path)[1]
            new_path = os.path.join(visit.folder, f"{note.number:02d} {safe_name(note.title)}{ext}")
            if not os.path.exists(new_path):
                os.replace(item.path, new_path)
                item.path = new_path


def write_report(visit, result):
    """Сохраняет отчёт.md в папку выезда — архив, по которому потом можно вспомнить всё."""
    photos = [i for i in visit.items if i.kind == "photo"]
    lines = [f"# Выезд: {visit.project} — {visit.place}, {visit.date}", "",
             "## Отчёт заказчику", "", result.client_report, "",
             "## Протокол", "", result.internal_summary, ""]
    if result.photos:
        lines += ["## Фото", ""]
        for note in result.photos:
            name = os.path.basename(photos[note.number - 1].path) if 1 <= note.number <= len(photos) else "?"
            lines.append(f"- **{name}** — {note.description}")
        lines.append("")
    if result.client_questions:
        lines += ["## Согласовать с заказчиком", ""]
        lines += [f"- {q.question} — {q.details}" for q in result.client_questions] + [""]
    if result.tasks:
        lines += ["## Задачи", ""]
        lines += [f"- {t.task} ({t.owner}{', до ' + t.due if t.due else ''})" for t in result.tasks] + [""]
    if result.unclear:
        lines += ["## Уточнить", ""] + [f"- {u}" for u in result.unclear] + [""]
    transcripts = [i for i in visit.items if i.transcript]
    if transcripts:
        lines += ["## Расшифровки", ""]
        lines += [f"- {i.time}, {os.path.basename(i.path)}: {i.transcript}" for i in transcripts] + [""]
    path = os.path.join(visit.folder, "отчёт.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def save_to_sheets(visit, result):
    """Записывает вопросы, задачи и события выезда в таблицу проекта. Возвращает текст-итог."""
    source = f"Выезд {visit.date}, {visit.place}"
    done = []
    if result.client_questions:
        ids = sheets.append_records(visit.project, "вопросы", [{
            "Дата открытия": visit.date, "Вопрос": q.question, "Что нужно решить": q.details,
            "Кто отвечает": q.owner, "Приоритет": q.priority, "Статус": "Открыт",
            "Блокирует этап": q.blocks_stage, "На что влияет": q.affects, "Источник / ссылка": source,
        } for q in result.client_questions], id_letter="Q")
        done.append(f"вопросы: {', '.join(ids)}")
    if result.tasks:
        ids = sheets.append_records(visit.project, "задачи", [{
            "Задача": t.task, "Ответственный": t.owner, "Срок": t.due, "Приоритет": t.priority,
            "Статус": "Не начата", "Следующий шаг": t.next_step, "Ссылка / примечание": source,
        } for t in result.tasks], id_letter="T")
        done.append(f"задачи: {', '.join(ids)}")
    events = [{"Дата": visit.date, "Тип": e.kind, "Содержание": e.content,
               "Ответственный / участник": e.participants, "Источник": source} for e in result.events]
    events.append({"Дата": visit.date, "Тип": "Событие", "Содержание": f"{source}: материалы и отчёт",
                   "Источник": source, "Комментарий": f"Папка: {visit.folder}"})
    sheets.append_records(visit.project, "журнал", events)
    done.append(f"журнал: {len(events)} записей")
    return "; ".join(done)
