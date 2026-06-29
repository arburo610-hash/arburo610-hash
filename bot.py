import os
from anthropic import Anthropic
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

client = Anthropic()

SYSTEM_PROMPT = """Ты — профессиональный SMM-специалист и маркетолог с 10-летним опытом.
Ты специализируешься на создании контента для Instagram.

Твои навыки:
- Пишешь цепляющие заголовки и подписи, которые останавливают прокрутку
- Знаешь психологию покупателя и триггеры вовлечённости
- Используешь сторителлинг, эмоции, призывы к действию
- Подбираешь релевантные хэштеги (популярные + нишевые)
- Адаптируешь тон под разные ниши (бьюти, фитнес, еда, бизнес, мода и т.д.)
- Пишешь на русском языке, живо и по-человечески

Формат ответа:
1. 📝 ТЕКСТ ПОСТА (готовый, можно сразу копировать)
2. #️⃣ ХЭШТЕГИ (20-30 штук, разбитые по группам)
3. 💡 СОВЕТ (короткая подсказка — когда лучше публиковать или что добавить к фото)

Если задача не ясна — уточни одним вопросом, не придумывай лишнего."""

user_histories = {}

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ Написать пост", "📋 Идеи для контента"],
        ["🔁 Переписать пост", "📊 Контент-план"],
        ["❓ Помощь"],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text(
        "Привет! 👋 Я твой личный SMM-агент.\n\n"
        "Я помогу тебе создавать крутой контент для Instagram — "
        "посты, хэштеги, идеи и контент-планы.\n\n"
        "Выбери действие или просто напиши мне задачу:",
        reply_markup=MENU_KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []

    if text == "✍️ Написать пост":
        await update.message.reply_text(
            "Отлично! Опиши мне:\n"
            "• О чём пост? (товар, услуга, событие, мысль)\n"
            "• Кто твоя аудитория?\n"
            "• Какой тон? (серьёзный, игривый, вдохновляющий)\n\n"
            "Или просто напиши задачу — разберёмся!"
        )
        return

    if text == "📋 Идеи для контента":
        await update.message.reply_text(
            "Для каких идей? Напиши мне:\n"
            "• Твоя ниша (например: кофейня, одежда, фитнес-тренер)\n"
            "• На сколько дней нужны идеи?"
        )
        return

    if text == "🔁 Переписать пост":
        await update.message.reply_text(
            "Вставь текст поста, который хочешь улучшить, "
            "и я перепишу его так, чтобы он работал лучше!"
        )
        return

    if text == "📊 Контент-план":
        await update.message.reply_text(
            "Для контент-плана скажи мне:\n"
            "• Твоя ниша\n"
            "• На сколько дней (7, 14, 30)?\n"
            "• Как часто публикуешь?"
        )
        return

    if text == "❓ Помощь":
        await update.message.reply_text(
            "Я умею:\n\n"
            "✍️ *Написать пост* — готовый текст + хэштеги\n"
            "📋 *Идеи для контента* — список тем для постов\n"
            "🔁 *Переписать пост* — улучшу твой текст\n"
            "📊 *Контент-план* — расписание постов на неделю/месяц\n\n"
            "Или просто пиши мне любой вопрос по контенту!",
            parse_mode="Markdown",
        )
        return

    await update.message.chat.send_action("typing")

    user_histories[user_id].append({"role": "user", "content": text})

    if len(user_histories[user_id]) > 20:
        user_histories[user_id] = user_histories[user_id][-20:]

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=user_histories[user_id],
        )
        reply = response.content[0].text
        user_histories[user_id].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply, reply_markup=MENU_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(
            "Упс, что-то пошло не так. Попробуй ещё раз! 🙏"
        )
        print(f"Ошибка: {e}")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Нет TELEGRAM_BOT_TOKEN в переменных окружения")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise ValueError("Нет ANTHROPIC_API_KEY в переменных окружения")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
