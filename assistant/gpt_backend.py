"""OpenAI Responses API backend for the Telegram design assistant.

No API request is made on import. OPENAI_API_KEY is required for AI operations.
All responses use store=False to avoid server-side conversation storage.
"""
import json
import logging
import os

from openai import OpenAI

LOG = logging.getLogger("assistant.gpt")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")


def _client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Не задан OPENAI_API_KEY в переменных тестового сервиса Railway")
    return OpenAI()


def _inputs(parts):
    """Convert existing Anthropic-style multimodal blocks to OpenAI input items."""
    converted = []
    for part in parts:
        if part.get("type") == "text":
            converted.append({"type": "input_text", "text": part["text"]})
        elif part.get("type") == "image":
            source = part["source"]
            if source.get("type") != "base64":
                raise ValueError("Ожидается изображение в base64")
            converted.append({"type": "input_image", "image_url": "data:"
                              + source["media_type"] + ";base64," + source["data"]})
    return converted


def parse_content(parts, schema, instructions=""):
    """Return a typed Pydantic object from text/images."""
    response = _client().responses.parse(
        model=MODEL,
        instructions=instructions or "Разбирай материалы точно. Ничего не выдумывай.",
        input=[{"role": "user", "content": _inputs(parts)}],
        text_format=schema,
        store=False,
    )
    if response.output_parsed is None:
        return None
    return response.output_parsed


def ask(history, user_text, instructions, anthropic_tools, dispatch, max_rounds=12):
    """Run function-call loop using existing read-only project tools.

    Write actions must be guarded by dispatch and require a separate approval.
    """
    tool_specs = [
        {"type": "function", "name": item["name"],
         "description": item["description"], "parameters": item["input_schema"]}
        for item in anthropic_tools
    ]
    items = list(history) + [{"role": "user", "content": user_text}]
    client = _client()
    for _ in range(max_rounds):
        response = client.responses.create(
            model=MODEL,
            instructions=instructions,
            input=items,
            tools=tool_specs,
            store=False,
        )
        calls = [part for part in response.output if part.type == "function_call"]
        if not calls:
            return response.output_text.strip() or "(пустой ответ)"
        items.extend(part.model_dump(exclude_none=True) for part in response.output)
        for call in calls:
            try:
                arguments = json.loads(call.arguments or "{}")
                result = dispatch(call.name, arguments)
                text_result = json.dumps(result, ensure_ascii=False, default=str)
            except Exception as exc:
                LOG.exception("Function call %s failed", call.name)
                text_result = json.dumps({"ошибка": str(exc)}, ensure_ascii=False)
            items.append({"type": "function_call_output", "call_id": call.call_id,
                          "output": text_result})
    return "Достигнут предел обращений к данным; уточните запрос."


def parse_calendar(text, schema, today):
    prompt = (
        "Составь только проект календарного события или напоминания из слов дизайнера. "
        "Не выдумывай дату или время. Если информации недостаточно, заполни пустыми строками "
        "и обозначь это в поле question. Даты: YYYY-MM-DD; время HH:MM; "
        "напоминание: YYYY-MM-DDTHH:MM, московское время. "
        "Сегодня по Москве: " + today + ". Запрос: " + text
    )
    return parse_content([{"type": "text", "text": prompt}], schema)
