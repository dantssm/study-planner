"""Дедлайни з Moodle Calendar (.ics).

Джерело задається в .env змінною MOODLE_ICS_URL:
  - посилання з Moodle (Календар -> Імпорт/експорт -> Експортувати -> Отримати URL), або
  - шлях до локального файлу, наприклад data/deadlines.ics (для тестів).
"""

import os
import urllib.request
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from icalendar import Calendar

load_dotenv()
SOURCE = os.getenv("MOODLE_ICS_URL") or "data/deadlines.ics"
TIMEZONE = ZoneInfo("Europe/Kyiv")

# Тип завдання визначаємо за словами в назві події
TYPE_KEYWORDS = {
    "quiz": ["квіз", "quiz", "тест"],
    "lab": ["лаб", "lab"],
    "essay": ["есе", "essay"],
    "project": ["проєкт", "проект", "project"],
}


def read_ics():
    if SOURCE.startswith("http"):
        with urllib.request.urlopen(SOURCE, timeout=20) as response:
            return response.read()
    with open(SOURCE, "rb") as f:
        return f.read()


def guess_type(title):
    for task_type, words in TYPE_KEYWORDS.items():
        if any(word in title.lower() for word in words):
            return task_type
    return "other"


def load_deadlines():
    """Список дедлайнів: id, курс, назва, тип, опис і час здачі (YYYY-MM-DDTHH:MM, Київ)."""
    deadlines = []
    for event in Calendar.from_ical(read_ics()).walk("VEVENT"):
        title = str(event.get("summary", ""))
        due = event.decoded("dtstart").astimezone(TIMEZONE)  # у Moodle час здачі = початок події
        deadlines.append({
            "id": str(event.get("uid")),
            "course": str(event.get("categories").cats[0]) if event.get("categories") else "",
            "title": title,
            "type": guess_type(title),
            "description": str(event.get("description", ""))[:300],
            "due": due.strftime("%Y-%m-%dT%H:%M"),
        })
    return deadlines
