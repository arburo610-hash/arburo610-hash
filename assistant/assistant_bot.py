"""Личный бизнес-ассистент Анастасии в Telegram — этап 1: «видит» все проекты.

Читает «Пульт управления проектами» и таблицы проектов в Google Sheets, отвечает на вопросы
(«что горит», «что ждём от заказчиков», «какие решения приняты по ЖК МОД»), присылает утреннюю сводку.
Только чтение: ничего в таблицах не меняет.
"""
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import sheets

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
        answer = ask_claude(history, user_text)
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
    await reply(update, context, update.message.text)


async def scheduled_brief(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        answer = ask_claude([], BRIEF_PROMPT)
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
