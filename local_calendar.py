"""Локальний календар у JSON-файлі — той самий інтерфейс, що й google_calendar.py.

Потрібен, щоб тести й розробка працювали без Google OAuth: CALENDAR_BACKEND=local.
"""

import json
import uuid
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent
STORE = BASE / "data" / "local_calendar.json"
INITIAL = BASE / "data" / "calendar_initial.json"
EVENT_KEYS = ("id", "title", "start", "end", "deadline_id")  # опис лише для людини, у відповіді його нема


def _load():
    if not STORE.exists():
        reset_calendar()
    return json.loads(STORE.read_text(encoding="utf-8"))["events"]


def _save(events):
    STORE.write_text(json.dumps({"events": events}, ensure_ascii=False, indent=2), encoding="utf-8")


def list_events(date_from, date_to):
    """Події з date_from по date_to включно (формат YYYY-MM-DD)."""
    # ValueError на поганий формат, як і в Google-версії. Нормалізуємо: fromisoformat приймає й «20261021»,
    # а порівняння нижче рядкове.
    date_from = date.fromisoformat(date_from).isoformat()
    date_to = date.fromisoformat(date_to).isoformat()
    events = [{k: e[k] for k in EVENT_KEYS} for e in _load() if date_from <= e["start"][:10] <= date_to]
    return sorted(events, key=lambda e: e["start"])


def add_event(title, start, end, deadline_id=None, description=""):
    events = _load()
    event_id = uuid.uuid4().hex[:12]
    events.append({"id": event_id, "title": title, "start": start, "end": end, "deadline_id": deadline_id,
                   "description": description})
    _save(events)
    return event_id


def reset_calendar():
    """Повертає календар до початкових подій (пари, робота, тренування)."""
    initial = json.loads(INITIAL.read_text(encoding="utf-8"))["events"]
    _save([{"id": e["id"], "title": e["title"], "start": e["start"], "end": e["end"], "deadline_id": None}
           for e in initial])
