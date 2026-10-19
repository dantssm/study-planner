"""Дедлайни з Moodle Calendar (.ics).

Джерело задається в .env змінною MOODLE_ICS_URL:
  - посилання з Moodle (Календар -> Імпорт/експорт -> Експортувати -> Отримати URL), або
  - шлях до локального файлу, наприклад data/deadlines.ics (для тестів).

Moodle експортує і «відкриття» (quiz opens), і «закриття» (quiz closes, is due) —
дедлайном вважаємо тільки друге. Останній успішний експорт кешується в data/deadlines.json.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from icalendar import Calendar

load_dotenv()
BASE = Path(__file__).parent
SOURCE = os.getenv("MOODLE_ICS_URL") or str(BASE / "data" / "deadlines.ics")
IS_URL = SOURCE.startswith("http")
CACHE = BASE / "data" / "deadlines.json"
TIMEZONE = ZoneInfo("Europe/Kyiv")
DESCRIPTION_LIMIT = 1000  # опис (бали, рубрика) потрібен агентові, щоб оцінити, скільки це займе

# Події, що лише відкривають доступ, — не дедлайни
OPENING_WORDS = ["opens", "opened", "відкрива", "доступно", "доступний", "available"]


def read_ics():
    if IS_URL:
        with urllib.request.urlopen(SOURCE, timeout=20) as response:
            return response.read()
    return (BASE / SOURCE).read_bytes()  # відносний шлях рахуємо від папки проєкту


def is_opening(title):
    return any(word in title.lower() for word in OPENING_WORDS)


def due_time(event):
    start = event.decoded("dtstart")
    if not isinstance(start, datetime):  # подія на весь день → кінець цього дня
        start = datetime.combine(start, time(23, 59), tzinfo=TIMEZONE)
    elif start.tzinfo is None:
        start = start.replace(tzinfo=TIMEZONE)
    return start.astimezone(TIMEZONE)


def find_link(description):
    """Перше посилання в тексті опису (деякі завдання Moodle кладе туди), без розділових знаків наприкінці."""
    match = re.search(r"https?://[^\s<>\"')\]]+", description)
    return match.group(0).rstrip(".,;:!") if match else None


def calendar_link(base, uid, due):
    """Посилання на день здачі в календарі Moodle (стандартний маршрут calendar/view.php). Потрібен base сайту."""
    event_id = uid.split("@")[0]
    if not base or not event_id.isdigit():
        return None
    timestamp = int(datetime.strptime(due, "%Y-%m-%dT%H:%M").replace(tzinfo=TIMEZONE).timestamp())
    return f"{base}/calendar/view.php?view=day&time={timestamp}#event_{event_id}"


def parse_deadlines(ics_bytes, base=None):
    deadlines = []
    for event in Calendar.from_ical(ics_bytes).walk("VEVENT"):
        title = str(event.get("summary", ""))
        if is_opening(title):
            continue
        categories = event.get("categories")
        description = " ".join(str(event.get("description", "")).split())
        uid, due = str(event.get("uid")), due_time(event).strftime("%Y-%m-%dT%H:%M")
        deadlines.append({
            "id": uid,
            "course": str(categories.cats[0]) if categories else "",
            "title": title,
            "description": description[:DESCRIPTION_LIMIT],
            "due": due,
            # Посилання для опису події в календарі; моделям їх не віддаємо (див. server.get_deadlines)
            "link": str(event["url"]) if event.get("url") else find_link(description),
            "calendar_link": calendar_link(base, uid, due),
        })
    return sorted(deadlines, key=lambda d: d["due"])


def load_deadlines():
    """Список дедлайнів: id, курс, назва, опис і час здачі (YYYY-MM-DDTHH:MM, Київ)."""
    site = urlparse(SOURCE)
    base = f"{site.scheme}://{site.netloc}" if IS_URL else None
    try:
        deadlines = parse_deadlines(read_ics(), base)
    except OSError:
        if IS_URL and CACHE.exists():  # Moodle недоступний → останній кеш
            return json.loads(CACHE.read_text(encoding="utf-8"))
        raise
    if IS_URL:
        CACHE.write_text(json.dumps(deadlines, ensure_ascii=False, indent=2), encoding="utf-8")
    return deadlines
