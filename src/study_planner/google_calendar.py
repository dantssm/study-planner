"""Робота з Google Calendar: вхід, читання і запис подій.

Перший запуск (один раз):  python google_calendar.py
  -> відкриється браузер: увійдіть і дозвольте доступ, з'явиться token.json
  -> тестовий календар заповниться початковими подіями з data/calendar_initial.json
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
TIMEZONE = "Europe/Kyiv"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]  # тільки події, не налаштування


def login():
    """Вхід через браузер. Зберігає токен у token.json."""
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open("token.json", "w") as f:
        f.write(creds.to_json())


def get_service():
    if not os.path.exists("token.json"):
        raise RuntimeError("Немає token.json. Спершу запустіть: python google_calendar.py")
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def to_google(local_time):
    # "2026-10-19T10:00" -> формат, який розуміє Google
    return {"dateTime": local_time + ":00", "timeZone": TIMEZONE}


def from_google(google_time):
    # "2026-10-19T10:00:00+03:00" -> "2026-10-19T10:00"
    return datetime.fromisoformat(google_time).astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%dT%H:%M")


def list_events(date_from, date_to):
    """Події з date_from по date_to включно (формат YYYY-MM-DD)."""
    tz = ZoneInfo(TIMEZONE)
    time_min = datetime.fromisoformat(date_from).replace(tzinfo=tz)
    time_max = datetime.fromisoformat(date_to).replace(tzinfo=tz) + timedelta(days=1)
    response = get_service().events().list(
        calendarId=CALENDAR_ID, timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=2500,
    ).execute()

    events = []
    for item in response.get("items", []):
        if "dateTime" not in item["start"]:
            continue  # події на весь день пропускаємо
        events.append({
            "id": item["id"],
            "title": item.get("summary", ""),
            "start": from_google(item["start"]["dateTime"]),
            "end": from_google(item["end"]["dateTime"]),
            # id дедлайну зберігаємо в прихованому полі події
            "deadline_id": item.get("extendedProperties", {}).get("private", {}).get("deadline_id"),
        })
    return events


def add_event(title, start, end, deadline_id=None):
    body = {"summary": title, "start": to_google(start), "end": to_google(end)}
    if deadline_id:
        body["extendedProperties"] = {"private": {"deadline_id": deadline_id}}
    created = get_service().events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return created["id"]


def reset_calendar():
    """Очищає тестовий календар і додає початкові події (пари, робота, тренування)."""
    # Запобіжник: працюємо тільки з окремим календарем, щоб не стерти особистий
    if not CALENDAR_ID.endswith("@group.calendar.google.com"):
        raise RuntimeError("GOOGLE_CALENDAR_ID має бути id окремого тестового календаря (…@group.calendar.google.com)")

    service = get_service()
    for event in list_events("2026-01-01", "2027-12-31"):
        service.events().delete(calendarId=CALENDAR_ID, eventId=event["id"]).execute()

    with open("data/calendar_initial.json", encoding="utf-8") as f:
        for event in json.load(f)["events"]:
            add_event(event["title"], event["start"], event["end"])


if __name__ == "__main__":
    if not os.path.exists("token.json"):
        login()
    reset_calendar()
    print("Готово: тестовий календар заповнено початковими подіями.")
