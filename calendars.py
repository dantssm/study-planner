"""Вибір календаря: Google (за замовчуванням) або локальний JSON для тестів.

CALENDAR_BACKEND=google | local  (змінна з .env або з середовища)

Контракт, який мають виконувати обидва календарі (google_calendar.py і local_calendar.py):
  list_events(date_from, date_to) -> [{id, title, start, end, deadline_id}]
      дати YYYY-MM-DD, діапазон включно; час подій YYYY-MM-DDTHH:MM у Києві;
      deadline_id заповнений лише в навчальних сесіях; поганий формат дати -> ValueError
  add_event(title, start, end, deadline_id=None, description="") -> id створеної події
      description — текст для людини (до чого готуємось, дедлайн, посилання); list_events його не повертає
  reset_calendar() -> повертає початкові події з data/calendar_initial.json
"""

import os

from dotenv import load_dotenv

load_dotenv()
BACKEND = os.getenv("CALENDAR_BACKEND", "google")

if BACKEND == "local":
    from local_calendar import add_event, list_events, reset_calendar
elif BACKEND == "google":
    from google_calendar import add_event, list_events, reset_calendar
else:
    raise RuntimeError(f"CALENDAR_BACKEND має бути google або local, а не {BACKEND!r}")

__all__ = ["BACKEND", "add_event", "list_events", "reset_calendar"]
