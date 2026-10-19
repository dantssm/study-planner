"""MCP-сервер `study_planner`: дедлайни з Moodle (.ics) + навчальні сесії в календарі.

Інструменти: get_deadlines, get_free_slots (читають) і schedule_session (пише).
Правила R1–R4 перевіряє сервер — модель не може їх порушити, навіть якщо помилиться.
Окремо запускати не треба — його запускає MCP-клієнт (див. agents.py).
"""

import csv
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer

import calendars
import moodle

MAX_HOURS_PER_DAY = 6                      # R3
DAY_START, DAY_END = "08:00", "23:00"      # R4
MAX_HOURS_PER_TASK = 12                    # R5: жорстка межа, щоб оцінка моделі не вилетіла в абсурд
MAX_RANGE_DAYS = 31                        # для get_free_slots: відповідь швидко росте
MAX_DEADLINE_RANGE_DAYS = 366              # дедлайнів мало, тож діапазон ширший
EARLIEST_BUFFER_MIN = 10                   # запас, щоб людина встигла схвалити план, поки слот не став минулим
PLAN_CSV = Path(__file__).parent / "study_plan.csv"  # журнал записаних сесій

mcp = MCPServer("study_planner")


def error(code, message, hint, retryable=True):
    """Помилка, з якої агент може вийти сам: що сталось, що робити далі і чи варто пробувати ще."""
    return {"error": code, "message": message, "hint": hint, "retryable": retryable}


def parse_range(date_from, date_to, max_days=MAX_RANGE_DAYS):
    """(from, to) як date або помилка-словник."""
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        return error("invalid_date", f"Неправильна дата: {date_from} / {date_to}",
                     "Формат: YYYY-MM-DD, наприклад 2026-10-19")
    if end < start:
        return error("invalid_date", "date_to раніше за date_from", "Поміняйте їх місцями")
    if (end - start).days >= max_days:
        return error("range_too_long", f"Діапазон довший за {max_days} днів",
                     "Запитуйте по тижню" if max_days == MAX_RANGE_DAYS else "Звузьте діапазон")
    return start, end


def now():
    """Поточний момент (R6). PLANNER_TODAY=YYYY-MM-DD фіксує «сьогодні» для тестів: увесь цей день — майбутнє."""
    fixed = os.getenv("PLANNER_TODAY")
    if fixed:
        return datetime.combine(date.fromisoformat(fixed), time.min)
    return datetime.now()


def earliest_start():
    """Найраніший початок, який пропонуємо: за ~10 хв від зараз, на сітці по 15 хв (16:06 → 16:30).
    R6 суворе (не раніше за зараз), але пропонуємо із запасом — інакше схвалений план встигає застаріти."""
    t = (now() + timedelta(minutes=EARLIEST_BUFFER_MIN)).replace(second=0, microsecond=0)
    return (t + timedelta(minutes=(-t.minute) % 15)).strftime("%Y-%m-%dT%H:%M")


def event_description(deadline):
    """Опис події в календарі: до чого готуємось, дедлайн і посилання. Складає сервер, тож модель не вигадає посилання."""
    lines = [f"Підготовка до: {deadline['title']}"]
    if deadline.get("course"):
        lines.append(f"Курс: {deadline['course']}")
    lines.append(f"Дедлайн: {deadline['due'].replace('T', ' ')}")
    if deadline.get("link"):
        lines.append(f"Завдання в Moodle: {deadline['link']}")
    elif deadline.get("calendar_link"):
        lines.append(f"Календар Moodle (день здачі): {deadline['calendar_link']}")
    return "\n".join(lines)


def hours_between(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600


def study_hours(events):
    """Скільки годин навчальних сесій (з deadline_id) серед цих подій."""
    return sum(hours_between(e["start"], e["end"]) for e in events if e["deadline_id"])


@mcp.tool()
def get_deadlines(date_from: str, date_to: str) -> dict:
    """Дедлайни з Moodle, у яких час здачі між date_from і date_to (YYYY-MM-DD, включно).

    Для кожного: id, курс, назва, опис (бали, рубрика, вимоги) і due (YYYY-MM-DDTHH:MM).
    Використовуй, щоб дізнатись, що і коли треба здати й скільки це приблизно займе."""
    parsed = parse_range(date_from, date_to, MAX_DEADLINE_RANGE_DAYS)
    if isinstance(parsed, dict):
        return parsed
    try:
        all_deadlines = moodle.load_deadlines()
    except OSError as e:
        return error("moodle_unavailable", f"Не вдалось прочитати Moodle: {e}",
                     "Спробуйте ще раз пізніше", retryable=False)
    # Посилання моделям не віддаємо: вони їм не потрібні, а текст із Moodle — недовірене джерело
    return {"deadlines": [{k: v for k, v in d.items() if k not in ("link", "calendar_link")}
                          for d in all_deadlines if date_from <= d["due"][:10] <= date_to]}


@mcp.tool()
def get_free_slots(date_from: str, date_to: str, min_minutes: int = 30) -> dict:
    """Вільні проміжки в календарі по днях між date_from і date_to (YYYY-MM-DD, включно).

    Показує тільки майбутній час (з запасом ~10 хв від зараз) між 08:00 і 23:00, не коротший за min_minutes
    (за замовчуванням 30).
    Для кожного дня також study_hours_left — скільки годин навчання ще можна додати (ліміт 6 год)."""
    parsed = parse_range(date_from, date_to)
    if isinstance(parsed, dict):
        return parsed
    if min_minutes < 1:
        return error("invalid_argument", "min_minutes має бути більше 0", "Наприклад, 30")
    first, last = parsed
    earliest = earliest_start()

    days = []
    for offset in range((last - first).days + 1):
        day = (first + timedelta(days=offset)).isoformat()
        events = calendars.list_events(day, day)
        cursor, free = max(f"{day}T{DAY_START}", earliest), []
        for e in sorted(events, key=lambda e: e["start"]):
            if e["start"] > cursor and (hours_between(cursor, e["start"]) * 60) >= min_minutes:
                free.append({"start": cursor[11:], "end": e["start"][11:]})
            cursor = max(cursor, e["end"])
        end_of_day = f"{day}T{DAY_END}"
        if cursor < end_of_day and hours_between(cursor, end_of_day) * 60 >= min_minutes:
            free.append({"start": cursor[11:], "end": DAY_END})
        days.append({"date": day, "free": free,
                     "study_hours_left": max(0, MAX_HOURS_PER_DAY - study_hours(events))})
    return {"days": days}


def check_rules(start, end, deadline_id, deadline):
    """Правила R1–R5 для сесії (start/end — YYYY-MM-DDTHH:MM). Повертає помилку-словник або None."""
    day, due = start[:10], deadline["due"]

    if end > due:  # R1
        return error("after_deadline", f"Сесія закінчується пізніше дедлайну ({due})",
                     f"Поставте сесію так, щоб вона закінчувалась до {due}")

    if start[11:] < DAY_START or end[11:] > DAY_END:  # R4
        return error("outside_hours", f"Сесія {start[11:]}–{end[11:]} поза {DAY_START}–{DAY_END}",
                     f"Обирайте час між {DAY_START} і {DAY_END}")

    day_events = calendars.list_events(day, day)
    for event in day_events:  # R2
        if start < event["end"] and event["start"] < end:
            return error("time_conflict", f"Перетин з «{event['title']}» ({event['start']}–{event['end']})",
                         f"Спробуйте до {event['start']} або після {event['end']}, "
                         "або подивіться get_free_slots")

    used, new = study_hours(day_events), hours_between(start, end)
    if used + new > MAX_HOURS_PER_DAY:  # R3
        return error("daily_limit", f"{day} уже {used:g} год навчання, з цією сесією було б {used + new:g}",
                     f"На цей день лишилось {MAX_HOURS_PER_DAY - used:g} год — скоротіть сесію або оберіть інший день")

    since = (date.fromisoformat(due[:10]) - timedelta(days=90)).isoformat()  # сесії бувають лише до дедлайну
    task_events = [e for e in calendars.list_events(since, due[:10]) if e["deadline_id"] == deadline_id]
    planned = study_hours(task_events)
    if planned + new > MAX_HOURS_PER_TASK:  # R5
        return error("task_budget_exceeded",
                     f"На «{deadline['title']}» уже заплановано {planned:g} год, з цією було б {planned + new:g}",
                     f"Ліміт {MAX_HOURS_PER_TASK} год на завдання — скоротіть сесію або не додавайте її")
    return None


def log_session(event_id, deadline_id, title, start, end):
    is_new = not PLAN_CSV.exists()
    with open(PLAN_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["written_at", "event_id", "deadline_id", "title", "start", "end"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), event_id, deadline_id, title, start, end])


@mcp.tool()
def schedule_session(title: str, start: str, end: str, deadline_id: str) -> dict:
    """Записує навчальну сесію в календар для завдання deadline_id (id з get_deadlines).

    start/end у форматі YYYY-MM-DDTHH:MM, в межах одного дня. Сервер відмовить з поясненням і
    підказкою, якщо сесія порушує правила: в минулому, після дедлайну, поза 08:00–23:00,
    перетинається з іншою подією, перевищує 6 годин навчання на день або 12 годин на одне завдання."""
    try:
        start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return error("invalid_time", f"Неправильний час: {start} / {end}",
                     "Формат: YYYY-MM-DDTHH:MM, наприклад 2026-10-21T18:00")
    if end_dt <= start_dt:
        return error("invalid_time", "Кінець сесії раніше за початок", "Поміняйте start і end місцями")
    if start_dt.date() != end_dt.date():
        return error("invalid_time", "Сесія має бути в межах одного дня", "Розбийте її на дві сесії по днях")
    start, end = start_dt.strftime("%Y-%m-%dT%H:%M"), end_dt.strftime("%Y-%m-%dT%H:%M")

    if start_dt < now():  # R6
        return error("in_the_past", f"Початок {start} уже в минулому (зараз {now():%Y-%m-%dT%H:%M})",
                     "Обирайте час у майбутньому — get_free_slots показує тільки його")

    try:
        deadlines = {d["id"]: d for d in moodle.load_deadlines()}
    except OSError as e:
        return error("moodle_unavailable", f"Не вдалось прочитати Moodle: {e}",
                     "Спробуйте ще раз пізніше", retryable=False)
    if deadline_id not in deadlines:
        return error("unknown_deadline", f"Немає дедлайну {deadline_id}",
                     f"Візьміть id з get_deadlines. Відомі: {list(deadlines)}")

    if rejection := check_rules(start, end, deadline_id, deadlines[deadline_id]):
        return rejection

    event_id = calendars.add_event(title, start, end, deadline_id, event_description(deadlines[deadline_id]))
    log_session(event_id, deadline_id, title, start, end)
    return {"event_id": event_id, "start": start, "end": end}


if __name__ == "__main__":
    mcp.run()
