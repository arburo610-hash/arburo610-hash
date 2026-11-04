"""Read-only, source-grounded management brief snapshot.

This module fetches the tables before asking GPT to summarize. An unavailable
project is explicitly marked unavailable: no task IDs or deadlines are inferred
from a project-count cell in the Pult.
"""
import datetime
import json
import logging
from zoneinfo import ZoneInfo

import reminders
import sheets

LOG = logging.getLogger("assistant.brief_snapshot")
TZ = ZoneInfo("Europe/Moscow")
SECTIONS = ("этапы", "задачи", "вопросы")
CLOSED = {"готово", "выполнено", "закрыта", "закрыт", "решён", "решен",
          "отменена", "отменено", "неактуально", "заменено", "устарело"}
INACTIVE = {"завершён", "завершен", "архив — на паузе"}


def _meaningful_rows(source, section, limit):
    raw = source["записи"]
    if section in ("задачи", "вопросы"):
        raw = [r for r in raw if str(
            r.get("Статус", r.get("Статус записи", ""))
        ).strip().casefold() not in CLOSED]
    # No invented order: retain the row order as found in the source.
    return raw[:limit], len(raw)


def gather():
    """Read all active project sections, preserving individual access failures."""
    now = datetime.datetime.now(TZ)
    projects = sheets.list_projects()  # A failure here aborts the brief.
    source_name = sheets.pult_read_source()
    output = {
        "дата": now.strftime("%Y-%m-%d %H:%M"),
        "время": "Москва",
        "источник_пульта": source_name,
        "тестовый_бриф": source_name == "ТЕСТОВЫЙ Пульт",
        "проекты": [],
        "события": [],
        "ошибки": []
    }
    for project in projects:
        name = project.get("Проект", "")
        status = project.get("Статус", "").strip().casefold()
        row = {
            "проект": name,
            "сведения_пульта": project,
            "источник": (
                "тестовая копия проекта" if source_name == "ТЕСТОВЫЙ Пульт"
                and name == "ЖК МОД" else "рабочая таблица проекта"
            ),
            "разделы": {},
            "доступ": "не проверен",
        }
        if status in INACTIVE or "архив" in status:
            row["доступ"] = "не проверялся: проект завершён или архивный"
            output["проекты"].append(row)
            continue
        errors = []
        for section in SECTIONS:
            try:
                src = sheets.read_project_section(name, section, only_open=False)
                # As of today, some sheets have fewer columns than others.
                if section == "этапы":
                    rows, count = _meaningful_rows(src, section, 12)
                else:
                    rows, count = _meaningful_rows(src, section, 35)
                row["разделы"][section] = {
                    "состояние": "доступен",
                    "всего_актуальных": count,
                    "записи": rows,
                    "показано": len(rows)
                }
            except Exception as exc:
                LOG.warning("Brief read failed project=%s section=%s type=%s",
                            name, section, type(exc).__name__)
                errors.append(section)
                row["разделы"][section] = {
                    "состояние": "недоступен",
                    "ошибка": type(exc).__name__,
                    "записи": []
                }
        row["доступ"] = (
            "все основные разделы доступны" if not errors
            else "неполный: недоступны " + ", ".join(errors)
        )
        output["проекты"].append(row)
    try:
        day0 = now.strftime("%Y-%m-%d")
        day1 = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        found = reminders.find(day0, day1, False)
        output["события"] = found[-30:]
    except Exception as exc:
        LOG.warning("Brief reminders read failed: %s", type(exc).__name__)
        output["ошибки"].append("Напоминания недоступны: " + type(exc).__name__)
    return output


def prompt(snapshot):
    """Constrain the model to an explicit, current, bounded source snapshot."""
    if not snapshot or not snapshot.get("проекты"):
        raise ValueError("Нет доступного Пульта — составление брифа запрещено")
    header = (
        "ТЕСТОВЫЙ БРИФ — не рабочий отчёт. "
        if snapshot.get("тестовый_бриф") else "Управленческий бриф. "
    )
    return (
        header
        + "Составь короткую русскоязычную управленческую сводку на основе ТОЛЬКО "
        "следующего проверенного снимка. Никаких дополнительных инструментов у тебя нет. "
        "Не придумывай действия, суммы, сроки, авансы, согласования, ID, задачи или причины блокеров. "
        "Для проекта с недоступными разделами явно напиши, что проверены только сведения Пульта, "
        "а детали задач и открытых вопросов не проверены. Не превращай счётчики Пульта в конкретные поручения. "
        "Для доступных разделов ссылаться на реальные ID; если в данных не указано, кто должен "
        "что-то сделать, не назначай ответственного самостоятельно. "
        "Если статус выполнения работ и официальный статус этапа различаются, "
        "объясни разницу; работы не означают закрытие без акта. "
        "Отдельно покажи: сегодня главное; по активным проектам подтверждённые шаги "
        "и ожидания; непроверенные источники; реальные события из напоминаний. "
        "Не проси согласовать новые действия и не выдавай тестовый бриф за рабочий. "
        "Не упоминай поле «В общем брифе», если его нет в данных. "
        "Если нет подтверждённых задач на сегодня, так и напиши. "
        "Пиши компактно, для телефона.\n\n"
        + json.dumps(snapshot, ensure_ascii=False, default=str)
    )
