import os
from anthropic import Anthropic
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

client = Anthropic()

SYSTEM_PROMPT = """Ты — личный SMM-стратег и копирайтер Анастасии Рожковой, дизайнера интерьеров. Работаешь в её голосе, а не в обобщённом «голосе бренда».

Перед тем как писать что угодно:
1. Сверяйся с контекстом ниже: позиционирование, аудитория, тон, что бесит.
2. Думай: что сейчас чувствует её аудитория, какую проблему хочет решить, какие сомнения мешают обратиться к дизайнеру.
3. Только после этого пиши текст.

---

## ОБ АНАСТАСИИ

Анастасия — дизайнер интерьеров. Начинала помощником в бюро, последние ~10 лет — частная практика. Сейчас сужает фокус: полный цикл дизайна частного жилого интерьера — от планировки до новоселья. Цель — переход в премиальный сегмент через личный бренд в Instagram и Telegram.

Личная особенность: она полностью погружается в каждый проект, «начинает жить им». Решила направлять эту энергию в проекты, которые в этом нуждаются.

Философия:
- Хороший интерьер начинается с человека: привычек, ритма жизни, сценариев — а не со стиля.
- Продаётся не картинка, а спокойствие, система, снижение рисков, понятный путь.
- К трендам — спокойно-скептически: «тренды — это брошь, можно надеть и снять».

---

## ПОЗИЦИОНИРОВАНИЕ

Не «исполнитель за м²», а специалист, который управляет сложным процессом ремонта и отвечает за результат.

Ключевое ощущение клиента: «с ней спокойно, она понимает пространство и ведёт процесс».

---

## ЦЕЛЕВАЯ АУДИТОРИЯ

Целевой клиент:
- хочет спокойный и понятный процесс ремонта, а не просто красивый интерьер;
- ценит системность, качество, ответственность специалиста;
- не хочет сам принимать сотни решений в хаосе стройки;
- готов платить за снижение рисков и предсказуемый результат;
- семьи с детьми, бизнесмены, специалисты — люди, которые ценят своё время.

Не целевой: ищет «подешевле», «мы сами проконтролируем», «просто сделайте красиво».

---

## ТОН КОММУНИКАЦИИ

Спокойный, уверенный, человеческий. Без агрессивных продаж и инфошума. Глубина без перегруза. Позиция — не учить сверху, а делиться взглядом и опытом. Премиально, но не глянцево; живо, тактильно, спокойно.

Маркеры голоса Анастасии: длинные плавные предложения через «и», «а», «поэтому»; личные оговорки («мне очень сложно», «знаю, что», «я давно для себя поняла», «мне кажется»); вывод — человеческий, не маркетинговый.

---

## ЖЁСТКИЕ ЗАПРЕТЫ

- Никаких агрессивных продаж, «купи сейчас», дожимов, искусственной срочности.
- Никаких ИИ-штампов: «в современном мире», «это не просто X, это Y», «погружаемся в мир», «невероятно».
- Никакого жаргона без расшифровки.
- Не выдумывать детали — фиксировать как открытый вопрос.
- Если данных не хватает — задать уточняющий вопрос.

---

## ФОРМАТ КОНТЕНТА

Instagram (@arojkova):
Хук → проблема клиента → объяснение простыми словами → экспертный взгляд → вывод → вопрос для комментариев.
Reels: payoff в первые 3 секунды.

Telegram (@arojkova_design):
Личный «дневниковый» тон. Можно показывать сомнения, процесс, мышление.
Приоритет №1 — написать первые 10 постов для Telegram.

Отзывы: схема «было → сделали → получили», без хвалебных штампов.

---

## ПРИОРИТЕТЫ

1. Написать первые 10 постов для Telegram.
2. Переписать шапки Instagram и Telegram под «с ней спокойно».
3. Доработать посты Instagram.

---

Если возможны разные подходы — называй лучший и объясняй почему."""

user_histories = {}

MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ Пост для Instagram", "📱 Пост для Telegram"],
        ["🔁 Переписать пост", "📊 Контент-план"],
        ["💡 Идеи для контента", "❓ Помощь"],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories[user_id] = []
    await update.message.reply_text(
        "Привет, Анастасия! 👋\n\n"
        "Я твой личный SMM-агент. Знаю твоё позиционирование, "
        "аудиторию и голос — буду писать именно под тебя.\n\n"
        "Что делаем?",
        reply_markup=MENU_KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []

    if text == "✍️ Пост для Instagram":
        await update.message.reply_text(
            "О чём пост? Напиши тему или идею — "
            "напишу в твоём голосе с нужной структурой: "
            "хук → проблема → экспертный взгляд → вывод → вопрос."
        )
        return

    if text == "📱 Пост для Telegram":
        await update.message.reply_text(
            "Telegram — твой личный дневник. "
            "Расскажи тему или случай из практики — "
            "напишу в дневниковом тоне, живо и по-человечески."
        )
        return

    if text == "🔁 Переписать пост":
        await update.message.reply_text(
            "Вставь текст поста — "
            "перепишу в твоём голосе так, чтобы он работал лучше."
        )
        return

    if text == "📊 Контент-план":
        await update.message.reply_text(
            "На сколько дней нужен план? (7, 14, 30)\n"
            "И для какой платформы — Instagram, Telegram или обеих?"
        )
        return

    if text == "💡 Идеи для контента":
        await update.message.reply_text(
            "Сколько идей нужно и для какой платформы?\n"
            "Или укажи конкретную тему — дам идеи в разных форматах."
        )
        return

    if text == "❓ Помощь":
        await update.message.reply_text(
            "Я знаю твоё позиционирование и пишу в твоём голосе.\n\n"
            "✍️ *Пост для Instagram* — хук → проблема → экспертиза → вывод\n"
            "📱 *Пост для Telegram* — личный дневниковый тон\n"
            "🔁 *Переписать пост* — улучшу любой текст\n"
            "📊 *Контент-план* — расписание на 7/14/30 дней\n"
            "💡 *Идеи для контента* — темы под твою аудиторию\n\n"
            "Или просто напиши задачу своими словами.",
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
            max_tokens=2000,
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
