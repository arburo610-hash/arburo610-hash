"""Личный бизнес-ассистент Анастасии в Telegram.

- Читает «Пульт управления проектами» и таблицы проектов в Google Sheets, отвечает на вопросы
  («что горит», «что ждём от заказчиков», «какие решения приняты по ЖК МОД»), присылает утреннюю сводку.
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

import media
import sheets
import visits

logging.basicConfig(level=logging.INFO)
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

Правила:
- Отвечай только по данным из таблиц. Чего в таблицах нет — так и скажи, не додумывай даты, суммы и договорённости.
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
]


def run_tool(name, args):
    if name == "list_projects":
        return sheets.list_projects()
    if name == "read_project":
        return sheets.read_project_section(args["project"], args["section"], args.get("only_open", True))
    raise ValueError(f"Неизвестный инструмент {name}")


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
        "/отмена — выйти из выезда без отчёта\n"
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


async def visit_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    visit = context.chat_data.get("visit")
    msg = update.message
    if visit is None:
        await msg.reply_text("Чтобы я разобрала материалы, сначала начните выезд: /выезд <проект> <место>")
        return

    if msg.photo:
        kind, tg_file, ext = "photo", msg.photo[-1], ".jpg"
    elif msg.video or msg.video_note:
        kind, tg_file, ext = "video", msg.video or msg.video_note, ".mp4"
    elif msg.voice or msg.audio:
        kind, tg_file, ext = "voice", msg.voice or msg.audio, ".ogg"
    elif msg.document and (msg.document.mime_type or "").startswith(("image/jpeg", "image/png")):
        kind, tg_file = "photo", msg.document
        ext = ".png" if msg.document.mime_type == "image/png" else ".jpg"
    elif msg.document and (msg.document.mime_type or "").startswith("video/"):
        kind, tg_file, ext = "video", msg.document, ".mp4"
    else:
        await msg.reply_text("Этот формат не понимаю. Фото пришлите как фото (не HEIC-файлом), видео — как видео.")
        return

    number = visit.count(kind) + 1
    names = {"photo": "фото", "video": "видео", "voice": "голосовое"}
    path = os.path.join(visit.folder, f"{names[kind]}_{number:02d}{ext}")
    try:
        f = await tg_file.get_file()
        await f.download_to_drive(path)
    except BadRequest as e:
        if "too big" in str(e).lower():
            await msg.reply_text(
                "Файл больше 20 МБ — Telegram не отдаёт такие ботам. Пришлите видео покороче "
                "или просто голосовое с тем, что в нём говорится."
            )
            return
        raise

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
    await update.message.reply_text(
        f"Разбираю: фото {visit.count('photo')}, видео {visit.count('video')}, "
        f"голосовых {visit.count('voice')}, заметок {visit.count('text')}. Пара минут…"
    )
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        result = await asyncio.to_thread(process_visit, visit, command_args(update))
    except Exception as e:
        log.exception("visit processing failed")
        await update.message.reply_text(f"Не получилось собрать отчёт: {e}\nМатериалы сохранены в {visit.folder}. Попробуйте /готово ещё раз.")
        return
    if result is None:
        await update.message.reply_text("Claude отказался обрабатывать материалы. Они сохранены в " + visit.folder)
        return

    await asyncio.to_thread(visits.apply_photo_names, visit, result)
    report_path = await asyncio.to_thread(visits.write_report, visit, result)
    context.chat_data["visit"] = None
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
