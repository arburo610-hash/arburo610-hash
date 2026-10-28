"""Личный бизнес-ассистент Анастасии в Telegram.

- Читает «Пульт управления проектами» и таблицы проектов в Google Sheets, отвечает на вопросы
  («что горит», «что ждём от заказчиков», «какие решения приняты по ЖК МОД»), присылает утреннюю сводку.
- Входящие: всё, что прислано вне выезда (фото, голосовое, видео, заметка текстом), сохраняется с подписью,
  чтобы потом найти, отнести к проекту и разобрать в отчёт (/разобрать).
- Режим выезда: принимает фото, видео, голосовые и заметки с объекта или от поставщика, делает отчёт заказчику,
  протокол, подписи к фото и — только после подтверждения кнопкой — записывает вопросы/задачи/события в таблицу проекта.
"""
import asyncio
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import inbox
import media
import sheets
import visits

logging.basicConfig(level=logging.INFO)
# httpx печатает каждый запрос к Telegram вместе с токеном бота — в журнал это не должно попадать
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("assistant")

MODEL = "claude-opus-5-5"
TZ = ZoneInfo("Europe/Moscow")
HISTORY_TURNS = 10  # сколько последних реплик помнить в диалоге
MAX_TOOL_ROUNDS = 12

client = anthropic.Anthropic()

SYSTEM_PROMPT = """Ты — личный бизнес-ассистент Анастасии Рожковой, дизайнера интерьеров (полный цикл жилого интерьера, ведёт проекты лично; в работе ArchiCAD, 3ds Max, Photoshop).

Твоя задача — держать в голове все её проекты и снимать с неё рутину: напоминать о сроках, видеть блокеры и зависшие ожидания, быстро находить принятые решения и открытые вопросы.

Источник правды — Google Sheets:
- «Пульт управления проектами» (инструмент list_projects): по строке на проект — статус, этап, ближайший срок, счётчики задач/ожиданий/блокеров.
- Таблица управления каждого проекта (инструмент read_project): разделы панель, паспорт, журнал, этапы, задачи, решения, вопросы, визуализатор, чертёжник.

Кроме таблиц, у Анастасии есть «Входящие» — быстрые заметки, которые она бросает на ходу: артикулы, образцы, контакты, идеи, фото и голосовые (инструменты save_note, list_notes, update_notes).
- Если сообщение — это информация, которую надо запомнить (артикул, размер, контакт, «понравилось…», «зафиксируй…»), а не вопрос, — сохрани её через save_note и коротко подтверди: что записала и к какому проекту. Ничего больше не делай.
- Проект указывай, только если он назван или очевиден. Заметка может относиться не к тому проекту, на котором она сейчас («была на МОД, увидела обои для Острова» — это Остров).
- На вопросы «что я сохраняла…», «какие обои для Острова…» отвечай по list_notes, с номерами заметок (#12).

Правила:
- Отвечай только по данным из таблиц и заметок. Чего в таблицах нет — так и скажи, не додумывай даты, суммы и договорённости.
- Ссылайся на ID записей (MOD-T-005, MOD-R-003), чтобы Анастасия могла быстро найти строку.
- Пиши по-русски, коротко и по делу, удобно для чтения с телефона: сначала главное, потом детали. Без канцелярита и воды.
- Если просят текст для заказчика или подрядчика — пиши спокойно, уважительно, по-человечески, от первого лица Анастасии.
- Сегодня: {today}."""

BRIEF_PROMPT = """Сделай утреннюю сводку по всем проектам. Структура:
1. 🔥 Горит — просроченные и ближайшие (на этой неделе) сроки, блокеры.
2. ⏳ Ждём от других — что ждём от заказчиков/подрядчиков и сколько уже ждём.
3. ✅ Мой фокус на сегодня — 3–5 конкретных шагов из задач с высоким приоритетом.
Проекты на паузе — одной строкой в конце. Загляни в задачи и вопросы тех проектов, где есть блокеры или близкие сроки."""

TOOLS = [
    {
        "name": "list_projects",
        "description": "Список всех проектов из Пульта управления: статус, текущий этап, ближайший срок, число активных задач, ожиданий, блокеров, решений, дата обновления.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_project",
        "description": "Читает раздел таблицы управления конкретного проекта. Проект указывается частью названия, как в Пульте (например «МОД», «Талисман», «Братьев Гранат»).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Название проекта или его часть"},
                "section": {"type": "string", "enum": list(sheets.SECTIONS)},
                "only_open": {
                    "type": "boolean",
                    "description": "true — скрыть закрытые записи (Готово, Решён, Неактуально…). По умолчанию true.",
                },
            },
            "required": ["project", "section"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_note",
        "description": "Сохраняет текстовую заметку во «Входящие» Анастасии: артикул, контакт, размер, идея — всё, что надо запомнить и разобрать позже.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст заметки — её словами, с артикулами и цифрами без изменений"},
                "project": {"type": "string", "description": "Проект, если назван или очевиден; иначе пустая строка"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "1–3 метки: обои, плитка, свет, мебель, контакт, идея…"},
            },
            "required": ["text", "project", "tags"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_notes",
        "description": "Ищет заметки во «Входящих» (текст, фото с описаниями, расшифровки голосовых).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Часть названия проекта или пусто — все проекты"},
                "status": {"type": "string", "enum": ["новая", "разобрана", "все"]},
                "query": {"type": "string", "description": "Слово для поиска: «обои», артикул, бренд. Пусто — без фильтра"},
            },
            "required": ["project", "status", "query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_notes",
        "description": "Меняет проект и/или статус заметок по номерам: «отнеси 3 и 5 к Острову», «отметь 7 как разобранную».",
        "input_schema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "integer"}},
                "project": {"type": "string", "description": "Новый проект или пусто — не менять"},
                "status": {"type": "string", "enum": ["новая", "разобрана", ""], "description": "Новый статус или пусто — не менять"},
            },
            "required": ["ids", "project", "status"],
            "additionalProperties": False,
        },
    },
]


def run_tool(name, args):
    if name == "list_projects":
        return sheets.list_projects()
    if name == "read_project":
        return sheets.read_project_section(args["project"], args["section"], args.get("only_open", True))
    if name == "save_note":
        note = inbox.add("text", text=args["text"], project=args.get("project", ""), tags=args.get("tags", []))
        return {"сохранено": f"#{note['id']}"}
    if name == "list_notes":
        status = "" if args.get("status") == "все" else args.get("status", inbox.NEW)
        notes = inbox.find(project=args.get("project", ""), status=status, query=args.get("query", ""))
        return {"найдено": len(notes), "заметки": [inbox.for_claude(n) for n in notes[-60:]]}
    if name == "update_notes":
        changed = inbox.update(args["ids"], project=args.get("project") or None, status=args.get("status") or None)
        return {"изменены": changed}
    raise ValueError(f"Неизвестный инструмент {name}")


def project_names():
    try:
        return [p.get("Проект", "") for p in sheets.list_projects()]
    except Exception:
        return []


def describe_media(kind, path, caption, transcript, frames):
    """Подпись к фото/видео/голосовому из «Входящих»: что это, артикулы, проект; или понимаем, что это вопрос."""
    content = [{"type": "text", "text": (
        f"Анастасия прислала ассистенту {dict(photo='фото', video='видео', voice='голосовое')[kind]} вне выезда. "
        f"Проекты: {', '.join(project_names()) or 'список недоступен'}.\n"
        f"Подпись: {caption or '(нет)'}\nРасшифровка речи: {transcript or '(нет)'}\n"
        "Разбери, что это. Только то, что видно или сказано, ничего не додумывай."
    )}]
    images = [path] if kind == "photo" else frames
    content += [media.image_block(p) for p in images[:4]]
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": content}],
        output_format=inbox.MediaNote,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return None
    return response.parsed_output


def ask_claude(history, user_text):
    """Агентный цикл: Claude сам решает, какие таблицы прочитать, и возвращает итоговый текст."""
    system = SYSTEM_PROMPT.format(today=datetime.datetime.now(TZ).strftime("%d.%m.%Y, %A"))
    messages = history + [{"role": "user", "content": user_text}]

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "Не получилось ответить на этот запрос — попробуйте сформулировать иначе."
        if response.stop_reason != "tool_use":
            return "\n".join(b.text for b in response.content if b.type == "text").strip() or "(пустой ответ)"

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                content = json.dumps(run_tool(block.name, block.input), ensure_ascii=False)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            except Exception as e:  # ошибку отдаём модели — она объяснит или попробует иначе
                log.exception("tool %s failed", block.name)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True})
        messages.append({"role": "user", "content": results})

    return "Запрос оказался слишком длинным — уточните, по какому проекту смотреть."


def allowed(update: Update) -> bool:
    ids = {i.strip() for i in os.environ.get("ALLOWED_TELEGRAM_IDS", "").split(",") if i.strip()}
    return str(update.effective_user.id) in ids


async def send_long(bot, chat_id, text):
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[i:i + 4000])


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    if not allowed(update):
        await update.message.reply_text(f"Доступ закрыт. Ваш Telegram ID: {update.effective_user.id}")
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    history = context.chat_data.setdefault("history", [])
    try:
        answer = await asyncio.to_thread(ask_claude, history, user_text)
    except anthropic.APIError as e:
        log.exception("Claude API error")
        await update.message.reply_text(f"Ошибка обращения к Claude: {e}")
        return
    history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": answer}]
    del history[:-HISTORY_TURNS * 2]
    await send_long(context.bot, update.effective_chat.id, answer)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я ваш бизнес-ассистент. Вижу Пульт и таблицы проектов.\n\n"
        "/сводка — что горит, чего ждём, фокус на сегодня\n"
        "/выезд МОД объект — начать выезд: дальше шлите фото, видео, голосовые, заметки\n"
        "/готово — собрать отчёт заказчику и протокол (можно с пожеланием: /готово коротко, акцент на розетках)\n"
        "/отмена — выйти из выезда без отчёта\n\n"
        "Вне выезда всё, что пришлёте (фото, голосовое, видео, «артикул обоев…»), я сохраню во «Входящие» с подписью.\n"
        "/входящие — что накопилось\n"
        "/разобрать МОД — собрать отчёт по заметкам проекта; /разобрать — разложить всё по проектам\n"
        "/з текст — записать заметку дословно, без обработки\n\n"
        "/сброс — начать разговор заново\n\n"
        "Или просто спросите: «что по ЖК МОД?», «какие решения по свету в Гранатах?», "
        "«набросай сообщение заказчице Талисмана про выезд»."
    )


async def brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, context, BRIEF_PROMPT)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["history"] = []
    await update.message.reply_text("Начали с чистого листа.")


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    visit = context.chat_data.get("visit")
    if visit and allowed(update):
        visit.items.append(visits.Item(kind="text", caption=update.message.text, time=now_hm()))
        await update.message.reply_text("📝 Записала в выезд")
        return
    await reply(update, context, update.message.text)


# ---------- Режим выезда ----------

def now_hm():
    return datetime.datetime.now(TZ).strftime("%H:%M")


def command_args(update: Update):
    parts = (update.message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def visit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    args = command_args(update).split(maxsplit=1)
    if not args:
        await update.message.reply_text("Напишите проект и место: /выезд МОД объект  или  /выезд Гранат мебельщики")
        return
    place = args[1] if len(args) > 1 else "объект"
    visit, known = await asyncio.to_thread(visits.start_visit, args[0], place)
    context.chat_data["visit"] = visit
    note = "" if known else "\n⚠️ Такого проекта нет в Пульте — материалы сохраню, но записать в таблицу не получится."
    await update.message.reply_text(
        f"Выезд начат: {visit.project} — {place}.{note}\n\n"
        "Шлите фото (можно из галереи, пачкой), видео, голосовые с комментариями, текстовые заметки. "
        "Подпись к фото тоже учитываю. Когда закончите — /готово."
    )


def media_kind(msg):
    """(вид, файл Telegram, расширение) или None, если формат не поддерживается."""
    if msg.photo:
        return "photo", msg.photo[-1], ".jpg"
    if msg.video or msg.video_note:
        return "video", msg.video or msg.video_note, ".mp4"
    if msg.voice or msg.audio:
        return "voice", msg.voice or msg.audio, ".ogg"
    mime = (msg.document.mime_type or "") if msg.document else ""
    if mime in ("image/jpeg", "image/png"):
        return "photo", msg.document, ".png" if mime == "image/png" else ".jpg"
    if mime.startswith("video/"):
        return "video", msg.document, ".mp4"
    return None


async def download(msg, tg_file, path):
    """Скачивает файл; False, если Telegram не отдаёт его (больше 20 МБ)."""
    try:
        f = await tg_file.get_file()
        await f.download_to_drive(path)
        return True
    except BadRequest as e:
        if "too big" in str(e).lower():
            await msg.reply_text(
                "Файл больше 20 МБ — Telegram не отдаёт такие ботам. Пришлите видео покороче "
                "или просто голосовое с тем, что в нём говорится."
            )
            return False
        raise


async def inbox_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото / видео / голосовое вне выезда — во «Входящие» с подписью. Голосовой вопрос — сразу в диалог."""
    msg = update.message
    found = media_kind(msg)
    if found is None:
        await msg.reply_text("Этот формат не понимаю. Фото пришлите как фото (не HEIC-файлом), видео — как видео.")
        return
    kind, tg_file, ext = found
    path = inbox.media_path(kind, ext)
    if not await download(msg, tg_file, path):
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    transcript, frames = "", []
    if kind in ("video", "voice"):
        transcript = await asyncio.to_thread(media.transcribe, path) or ""
        if kind == "video":
            frames = await asyncio.to_thread(media.video_frames, path, inbox.INBOX_DIR)
    try:
        info = await asyncio.to_thread(describe_media, kind, path, msg.caption or "", transcript, frames)
    except anthropic.APIError:
        log.exception("describe failed")
        info = None  # заметку всё равно сохраняем, просто без подписи

    if kind == "voice" and info and info.is_request and transcript:
        os.remove(path)
        await reply(update, context, transcript)
        return

    note = inbox.add(kind, path=path, caption=msg.caption or "", transcript=transcript, frames=frames,
                     summary=info.summary if info else "", details=info.details if info else "",
                     project=info.project if info else "", tags=info.tags if info else [])
    answer = f"📥 Во входящих {inbox.short(note)}"
    if note["details"]:
        answer += f"\n{note['details'][:600]}"
    if not note["project"]:
        answer += f"\n\nК какому проекту? Можно ответить словами: «#{note['id']} — Остров»."
    await msg.reply_text(answer)


async def visit_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    visit = context.chat_data.get("visit")
    msg = update.message
    if visit is None:
        await inbox_media(update, context)
        return

    found = media_kind(msg)
    if found is None:
        await msg.reply_text("Этот формат не понимаю. Фото пришлите как фото (не HEIC-файлом), видео — как видео.")
        return
    kind, tg_file, ext = found

    number = visit.count(kind) + 1
    names = {"photo": "фото", "video": "видео", "voice": "голосовое"}
    path = os.path.join(visit.folder, f"{names[kind]}_{number:02d}{ext}")
    if not await download(msg, tg_file, path):
        return

    item = visits.Item(kind=kind, path=path, caption=msg.caption or "", time=now_hm())
    if kind in ("video", "voice"):
        await context.bot.send_chat_action(update.effective_chat.id, "typing")
        item.transcript = await asyncio.to_thread(media.transcribe, path) or ""
        if kind == "video":
            item.frames = await asyncio.to_thread(media.video_frames, path, visit.folder)
    visit.items.append(item)

    if kind == "photo":
        if visit.count("photo") % 10 == 1:  # не спамим на каждую фотографию из пачки
            await msg.reply_text(f"📷 Принимаю фото ({visit.count('photo')}…)")
    elif item.transcript:
        await msg.reply_text(f"🎙 Расшифровала: {item.transcript[:500]}")
    else:
        await msg.reply_text("Сохранила. Расшифровка не настроена — добавьте пару слов текстом, о чём это.")


async def visit_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    visit = context.chat_data.get("visit")
    if visit is None or not visit.items:
        await update.message.reply_text("Нет активного выезда с материалами. Начните: /выезд <проект> <место>")
        return
    if await run_report(update, context, visit, command_args(update)):
        context.chat_data["visit"] = None


async def run_report(update: Update, context: ContextTypes.DEFAULT_TYPE, visit, extra_instruction):
    """Отчёт заказчику, протокол, подписи к фото, файл и кнопка записи в таблицу. True — если получилось."""
    await update.message.reply_text(
        f"Разбираю: фото {visit.count('photo')}, видео {visit.count('video')}, "
        f"голосовых {visit.count('voice')}, заметок {visit.count('text')}. Пара минут…"
    )
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        result = await asyncio.to_thread(process_visit, visit, extra_instruction)
    except Exception as e:
        log.exception("visit processing failed")
        await update.message.reply_text(f"Не получилось собрать отчёт: {e}\nМатериалы сохранены, попробуйте ещё раз.")
        return False
    if result is None:
        await update.message.reply_text("Claude отказался обрабатывать материалы. Они сохранены, попробуйте переформулировать.")
        return False

    await asyncio.to_thread(visits.apply_photo_names, visit, result)
    report_path = await asyncio.to_thread(visits.write_report, visit, result)
    context.chat_data["pending_visit"] = (visit, result)

    chat = update.effective_chat.id
    await context.bot.send_message(chat, "✉️ Отчёт для заказчика (перешлите или скопируйте в Telegram / MAX):")
    await send_long(context.bot, chat, result.client_report)

    summary = [f"📋 Протокол\n{result.internal_summary}"]
    if result.client_questions:
        summary.append("❓ Согласовать с заказчиком:\n" + "\n".join(f"• {q.question}" for q in result.client_questions))
    if result.tasks:
        summary.append("✅ Задачи:\n" + "\n".join(f"• {t.task} — {t.owner}" for t in result.tasks))
    if result.unclear:
        summary.append("🤔 Уточните, пожалуйста:\n" + "\n".join(f"• {u}" for u in result.unclear))
    summary.append("📁 Фото подписаны и переименованы. Полный отчёт с подписями ко всем фото и расшифровками — в файле ниже.")
    await send_long(context.bot, chat, "\n\n".join(summary))
    with open(report_path, "rb") as f:
        await context.bot.send_document(
            chat, f, filename=f"Выезд {visit.date} {visits.safe_name(visit.project)} {visits.safe_name(visit.place)}.md")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Записать в таблицу проекта", callback_data="visit_save"),
        InlineKeyboardButton("Не записывать", callback_data="visit_skip"),
    ]])
    await context.bot.send_message(
        chat,
        f"Добавить в таблицу «{visit.project}»: вопросов {len(result.client_questions)}, "
        f"задач {len(result.tasks)}, событий в журнал {len(result.events) + 1}?",
        reply_markup=keyboard,
    )
    # чтобы можно было попросить «сделай отчёт короче» обычным сообщением
    history = context.chat_data.setdefault("history", [])
    history += [{"role": "user", "content": f"Обработай выезд: {visit.project}, {visit.place}, {visit.date}"},
                {"role": "assistant", "content": f"Отчёт заказчику:\n{result.client_report}\n\nПротокол:\n{result.internal_summary}"}]
    return True


# ---------- Входящие ----------

async def note_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/з текст — записать дословно, без Claude."""
    if not allowed(update):
        return
    body = command_args(update)
    if not body:
        await update.message.reply_text("Напишите заметку после команды: /з обои Arte 12345 для Острова")
        return
    note = await asyncio.to_thread(inbox.add, "text", text=body)
    await update.message.reply_text(f"📥 Записала #{note['id']}. Проект и метки можно добавить при разборе.")


async def inbox_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    notes = await asyncio.to_thread(inbox.find)
    if not notes:
        await update.message.reply_text("Во входящих пусто — всё разобрано.")
        return
    groups = {}
    for n in notes:
        groups.setdefault(n["project"] or "Без проекта", []).append(n)
    parts = [f"📥 Неразобранных заметок: {len(notes)}"]
    for project, items in groups.items():
        parts.append(f"\n{project} ({len(items)}):\n" + "\n".join(inbox.short(n) for n in items))
    parts.append("\n/разобрать <проект> — отчёт по заметкам проекта; /разобрать — разложить по проектам.")
    await send_long(context.bot, update.effective_chat.id, "\n".join(parts))


async def inbox_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/разобрать МОД — отчёт по новым заметкам проекта; без проекта — Claude раскладывает всё по проектам."""
    if not allowed(update):
        return
    args = command_args(update)
    if not args:
        await reply(update, context,
                    "Разбери мои новые заметки во «Входящих»: сгруппируй по проектам, для заметок без проекта предложи проект "
                    "(сверься с Пультом), отметь, что похоже на вопрос заказчику или задачу. Проекты сам не меняй — "
                    "спроси, подтверждаю ли я раскладку.")
        return
    project_query, _, extra = args.partition(" ")
    try:
        project = await asyncio.to_thread(sheets.find_project, project_query)
    except Exception:  # Пульт недоступен — разбираем по названию как есть
        project = None
    project_title = project["Проект"] if project else project_query
    notes = await asyncio.to_thread(inbox.find, project_query)
    if not notes:
        await update.message.reply_text(f"Новых заметок по «{project_title}» нет. /входящие — посмотреть все.")
        return
    visit = await asyncio.to_thread(inbox.to_visit, notes, project_title)
    instruction = ("Это не один выезд, а заметки, накопленные за разные дни (дата у каждой). "
                   "Отчёт заказчику делай только если в заметках есть то, что ему стоит сообщить; иначе напиши в client_report "
                   "коротко, что для заказчика пока ничего нет. " + extra)
    if await run_report(update, context, visit, instruction):
        await asyncio.to_thread(inbox.update, [n["id"] for n in notes], status=inbox.DONE)


def process_visit(visit, extra_instruction):
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT.format(today=datetime.datetime.now(TZ).strftime("%d.%m.%Y, %A")),
        messages=[{"role": "user", "content": visits.build_content(visit, extra_instruction)}],
        output_format=visits.VisitResult,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return None
    return response.parsed_output


async def visit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    visit = context.chat_data.pop("visit", None)
    await update.message.reply_text(f"Выезд закрыт без отчёта. Файлы остались в {visit.folder}" if visit else "Активного выезда нет.")


async def visit_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        return
    pending = context.chat_data.pop("pending_visit", None)
    if pending is None:
        await query.edit_message_text("Это предложение уже неактуально.")
        return
    if query.data == "visit_skip":
        await query.edit_message_text("Ок, в таблицу не записываю. Отчёт и фото остались в папке выезда.")
        return
    visit, result = pending
    try:
        done = await asyncio.to_thread(visits.save_to_sheets, visit, result)
    except Exception as e:
        log.exception("save to sheets failed")
        context.chat_data["pending_visit"] = pending  # можно нажать ещё раз после исправления доступа
        await query.edit_message_text(
            f"Не удалось записать: {e}\nПроверьте, что сервисному аккаунту дан доступ «Редактор» к таблице проекта.",
            reply_markup=query.message.reply_markup,
        )
        return
    await query.edit_message_text(f"Записала в «{visit.project}» — {done}.")


async def scheduled_brief(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        answer = await asyncio.to_thread(ask_claude, [], BRIEF_PROMPT)
    except anthropic.APIError as e:
        answer = f"Утренняя сводка не собралась: {e}"
    await send_long(context.bot, chat_id, "Доброе утро! " + answer)


def main():
    token = os.environ["ASSISTANT_TELEGRAM_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    # Telegram принимает в командах только латиницу; русские названия — как подсказка в /start
    app.add_handler(CommandHandler(["brief", "svodka"], brief))
    app.add_handler(MessageHandler(filters.Regex(r"^/сводка"), brief))
    app.add_handler(CommandHandler(["reset"], reset))
    app.add_handler(MessageHandler(filters.Regex(r"^/сброс"), reset))
    app.add_handler(CommandHandler(["visit", "vyezd"], visit_start))
    app.add_handler(MessageHandler(filters.Regex(r"^/выезд"), visit_start))
    app.add_handler(CommandHandler(["done", "gotovo"], visit_finish))
    app.add_handler(MessageHandler(filters.Regex(r"^/готово"), visit_finish))
    app.add_handler(CommandHandler(["cancel", "otmena"], visit_cancel))
    app.add_handler(MessageHandler(filters.Regex(r"^/отмена"), visit_cancel))
    app.add_handler(CommandHandler(["note", "z"], note_quick))
    app.add_handler(MessageHandler(filters.Regex(r"^/з(\s|$)"), note_quick))
    app.add_handler(CommandHandler(["inbox", "vhodyashie"], inbox_list))
    app.add_handler(MessageHandler(filters.Regex(r"^/входящие"), inbox_list))
    app.add_handler(CommandHandler(["sort", "razobrat"], inbox_sort))
    app.add_handler(MessageHandler(filters.Regex(r"^/разобрать"), inbox_sort))
    app.add_handler(CallbackQueryHandler(visit_decision, pattern=r"^visit_"))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO | filters.Document.ALL,
        visit_media,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))

    brief_chat = os.environ.get("BRIEF_CHAT_ID")
    if brief_chat:
        hh, mm = os.environ.get("BRIEF_TIME", "09:00").split(":")
        app.job_queue.run_daily(
            scheduled_brief,
            time=datetime.time(int(hh), int(mm), tzinfo=TZ),
            days=(1, 2, 3, 4, 5),  # пн–пт (в PTB 0 — воскресенье)
            chat_id=int(brief_chat),
        )

    log.info("Ассистент запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
