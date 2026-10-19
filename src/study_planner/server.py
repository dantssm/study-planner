"""MCP-сервер `study_planner`: дедлайни з Moodle (.ics) + навчальні блоки в Google Calendar.

Окремо запускати не треба — його запускає MCP-клієнт (див. planner.py).
"""

from datetime import datetime

from mcp.server.mcpserver import MCPServer

import google_calendar
import moodle

# Припущення: скільки годин займає завдання кожного типу
EST_HOURS = {"quiz": 1, "lab": 4, "essay": 3, "project": 8, "other": 2}

mcp = MCPServer("study_planner")


def error(code, message, hint):
    # Помилка, з якої агент може вийти сам: що сталось + що робити далі
    return {"error": code, "message": message, "hint": hint}


@mcp.tool()
def get_deadlines(date_from: str, date_to: str) -> dict:
    """Дедлайни з Moodle між date_from і date_to (формат YYYY-MM-DD): опис завдання і оцінка годин."""
    result = []
    for d in moodle.load_deadlines():
        day = d["due"][:10]  # "2026-10-21T23:59" -> "2026-10-21"
        if date_from <= day <= date_to:
            d["est_hours"] = EST_HOURS[d["type"]]
            result.append(d)
    return {"deadlines": result}


@mcp.tool()
def get_events(date_from: str, date_to: str) -> dict:
    """Події в календарі між date_from і date_to (формат YYYY-MM-DD).

    Навчальні блоки мають заповнене поле deadline_id, пари й інші справи — ні.
    """
    try:
        return {"events": google_calendar.list_events(date_from, date_to)}
    except ValueError:
        return error("invalid_date", f"Неправильна дата: {date_from} / {date_to}",
                     "Формат: YYYY-MM-DD, наприклад 2026-10-19")


@mcp.tool()
def create_study_block(title: str, start: str, end: str, deadline_id: str) -> dict:
    """Записує навчальний блок у календар. start/end у форматі YYYY-MM-DDTHH:MM."""
    # 1. Перевіряємо формат часу
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return error("invalid_time", f"Неправильний час: {start} / {end}",
                     "Формат: YYYY-MM-DDTHH:MM, наприклад 2026-10-21T18:00")
    if end_dt <= start_dt:
        return error("invalid_time", "Кінець блоку раніше за початок", "Поміняйте start і end місцями")

    # 2. Перевіряємо, що такий дедлайн існує
    deadlines = {d["id"]: d for d in moodle.load_deadlines()}
    if deadline_id not in deadlines:
        return error("unknown_deadline", f"Немає дедлайну {deadline_id}",
                     f"Доступні id: {list(deadlines)}")

    # 3. Правило R1: блок має закінчитись до дедлайну
    due = deadlines[deadline_id]["due"]
    if end_dt > datetime.fromisoformat(due):
        return error("after_deadline", f"Блок закінчується пізніше дедлайну ({due})",
                     f"Поставте блок раніше {due}")

    # 4. Правило R2: блок не перетинається з іншими подіями цього дня
    for event in google_calendar.list_events(start[:10], end[:10]):
        ev_start = datetime.fromisoformat(event["start"])
        ev_end = datetime.fromisoformat(event["end"])
        if start_dt < ev_end and ev_start < end_dt:
            return error("time_conflict", f"Перетин з «{event['title']}» ({event['start']}–{event['end']})",
                         f"Спробуйте до {event['start']} або після {event['end']}")

    # 5. Усе добре — записуємо подію в календар
    start = start_dt.strftime("%Y-%m-%dT%H:%M")
    end = end_dt.strftime("%Y-%m-%dT%H:%M")
    event_id = google_calendar.add_event(title, start, end, deadline_id)
    return {"event_id": event_id, "start": start, "end": end}


if __name__ == "__main__":
    mcp.run()
