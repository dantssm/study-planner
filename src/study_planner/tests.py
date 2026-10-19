"""Оцінка точності: 5 тестових запитів через мультиагентну систему і через одного агента.

Перед кожним тестом календар скидається до початкового стану.
Тест пройдено, якщо в підсумковому календарі виконані всі правила
і кожне потрібне завдання отримало свої години.

Запуск (з папки code):
  python tests.py               # усі 5 тестів, обидві системи
  python tests.py multi         # тільки два агенти
  python tests.py single 4 5    # тільки один агент, тести 4 і 5
Результати накопичуються в results.json, таблиця — у консолі і в results.md
"""

import asyncio
import json
import os
import sys
from datetime import datetime

# Тести завжди на однакових даних: дедлайни з локального .ics і фіксоване "сьогодні".
# Задаємо до імпорту planner/moodle, щоб вони підхопили ці значення.
os.environ["MOODLE_ICS_URL"] = "data/deadlines.ics"
os.environ["PLANNER_TODAY"] = "2026-10-19"

from mcp import ClientSession
from mcp.client.stdio import stdio_client

import google_calendar
import moodle
from planner import SERVER, run_multi, run_single

TESTS = [
    {"id": 1, "request": "Сплануй мені підготовку до всіх дедлайнів цього тижня (19–25 жовтня).",
     "need_hours": {"d1": 1, "d2": 4, "d3": 3}, "free_days": []},
    {"id": 2, "request": "Сплануй підготовку до дедлайнів цього тижня. У середу 21 жовтня я зайнята весь день.",
     "need_hours": {"d1": 1, "d2": 4, "d3": 3}, "free_days": ["2026-10-21"]},
    {"id": 3, "request": "Постав 2 години на Лабораторну 2 з Генеративного ШІ в суботу 24 жовтня о 10:00.",
     "need_hours": {"d2": 2}, "free_days": []},  # субота пізніше дедлайну -> after_deadline
    {"id": 4, "request": "Додай 3 години на есе з Баз даних у четвер 22 жовтня з 12:00.",
     "need_hours": {"d3": 3}, "free_days": []},  # о 13:00 семінар -> time_conflict
    {"id": 5, "request": "Постав по 4 години на кожне завдання цього тижня, все у вівторок 20 жовтня.",
     "need_hours": {}, "free_days": []},  # 12 годин в один день порушує R3
]


def check(test, events):
    """Повертає список порушень. Порожній список = тест пройдено."""
    due = {d["id"]: d["due"] for d in moodle.load_deadlines()}
    blocks = [e for e in events if e["deadline_id"]]
    problems = []

    hours_per_day, hours_per_deadline = {}, {}
    for b in blocks:
        start, end = datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])
        hours = (end - start).total_seconds() / 3600
        day = b["start"][:10]
        hours_per_day[day] = hours_per_day.get(day, 0) + hours
        hours_per_deadline[b["deadline_id"]] = hours_per_deadline.get(b["deadline_id"], 0) + hours

        if end > datetime.fromisoformat(due[b["deadline_id"]]):
            problems.append(f"R1: «{b['title']}» після дедлайну")
        if b["start"][11:] < "08:00" or b["end"][11:] > "23:00" or b["end"][:10] != day:
            problems.append(f"R4: «{b['title']}» поза 08:00–23:00")
        if day in test["free_days"]:
            problems.append(f"«{b['title']}» у день, коли користувач зайнятий")

    # R2: жодні дві події не перетинаються
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if a["start"] < b["end"] and b["start"] < a["end"]:
                problems.append(f"R2: «{a['title']}» перетинається з «{b['title']}»")

    for day, hours in hours_per_day.items():
        if hours > 6:
            problems.append(f"R3: {day} заплановано {hours:g} год навчання")

    for deadline_id, hours in test["need_hours"].items():
        if hours_per_deadline.get(deadline_id, 0) < hours:
            problems.append(f"R5: для {deadline_id} заплановано {hours_per_deadline.get(deadline_id, 0):g} з {hours} год")

    return problems


def load_results():
    if os.path.exists("results.json"):
        with open("results.json", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_table(results):
    """Таблиця з усіх запусків, що є в results.json."""
    lines = ["| Тест | Два агенти | Один агент |", "|---|---|---|"]
    for test in TESTS:
        cells = []
        for system in ["multi", "single"]:
            problems = results.get(f"{test['id']}-{system}")
            if problems is None:
                cells.append("— (не запускали)")
            else:
                cells.append("✅" if not problems else "❌ " + "; ".join(problems))
        lines.append(f"| {test['id']}. {test['request']} | {cells[0]} | {cells[1]} |")

    for system, label in [("multi", "Два агенти"), ("single", "Один агент")]:
        runs = [results[f"{t['id']}-{system}"] for t in TESTS if f"{t['id']}-{system}" in results]
        passed = sum(not problems for problems in runs)
        lines.append(f"\n**{label}: {passed}/{len(runs)}**")

    table = "\n".join(lines)
    print("\n" + table)
    with open("results.md", "w", encoding="utf-8") as f:
        f.write(table + "\n")


async def main():
    # Які системи й тести запускати (див. приклади на початку файлу)
    systems = [s for s in ["multi", "single"] if s in sys.argv] or ["multi", "single"]
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()] or [t["id"] for t in TESTS]
    runners = {"multi": run_multi, "single": run_single}

    results = load_results()
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for system in systems:
            for test in TESTS:
                if test["id"] not in ids:
                    continue
                print(f"\n########## Тест {test['id']} · {system} ##########")
                google_calendar.reset_calendar()
                await runners[system](test["request"], session)
                events = google_calendar.list_events("2026-10-19", "2026-10-26")
                results[f"{test['id']}-{system}"] = check(test, events)
                with open("results.json", "w", encoding="utf-8") as f:  # зберігаємо після кожного тесту
                    json.dump(results, f, ensure_ascii=False, indent=2)

    save_table(results)
    from planner import usage
    print(f"Gemini: {usage['requests']} запитів, {usage['tokens']} токенів")


if __name__ == "__main__":
    asyncio.run(main())
