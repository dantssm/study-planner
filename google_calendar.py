"""Робота з Google Calendar: вхід, читання і запис подій.

Перший запуск (один раз):  uv run python google_calendar.py
  -> відкриється браузер: увійдіть і дозвольте доступ, з'явиться token.json
  -> тестовий календар заповниться початковими подіями з data/calendar_initial.json
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()
BASE = Path(__file__).parent
TOKEN = BASE / "token.json"
CREDENTIALS = BASE / "credentials.json"
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
TIMEZONE = "Europe/Kyiv"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]  # тільки події, не налаштування


def save_token(creds):
    # У токені є refresh_token і client_secret, тому доступ лише для власника файлу
    TOKEN.touch(mode=0o600, exist_ok=True)
    TOKEN.chmod(0o600)
    TOKEN.write_text(creds.to_json())


def login():
    """Вхід через браузер. Зберігає токен у token.json."""
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    save_token(flow.run_local_server(port=0))


def get_service():
    if not TOKEN.exists():
        raise RuntimeError("Немає token.json. Спершу запустіть: uv run python google_calendar.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_token(creds)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def to_google(local_time):
    # "2026-10-19T10:00" → формат Google
    return {"dateTime": local_time + ":00", "timeZone": TIMEZONE}


def from_google(google_time):
    # "2026-10-19T10:00:00+03:00" → "2026-10-19T10:00"
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
            continue  # події на весь день не враховуємо
        events.append({
            "id": item["id"],
            "title": item.get("summary", ""),
            "start": from_google(item["start"]["dateTime"]),
            "end": from_google(item["end"]["dateTime"]),
            "deadline_id": item.get("extendedProperties", {}).get("private", {}).get("deadline_id"),
        })
    return events


def add_event(title, start, end, deadline_id=None, description=""):
    body = {"summary": title, "start": to_google(start), "end": to_google(end)}
    if description:
        body["description"] = description  # Google сам робить посилання в тексті клікабельними
    if deadline_id:  # зберігаємо в прихованому полі події
        body["extendedProperties"] = {"private": {"deadline_id": deadline_id}}
    created = get_service().events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return created["id"]


def reset_calendar():
    """Очищає тестовий календар і додає початкові події (пари, робота, тренування)."""
    if not CALENDAR_ID.endswith("@group.calendar.google.com"):  # запобіжник: щоб не стерти особистий календар
        raise RuntimeError("GOOGLE_CALENDAR_ID має бути id окремого тестового календаря (…@group.calendar.google.com)")

    service = get_service()
    for event in list_events("2026-01-01", "2027-12-31"):
        service.events().delete(calendarId=CALENDAR_ID, eventId=event["id"]).execute()

    with open(BASE / "data" / "calendar_initial.json", encoding="utf-8") as f:
        for event in json.load(f)["events"]:
            add_event(event["title"], event["start"], event["end"])


if __name__ == "__main__":
    if not TOKEN.exists():
        login()
    reset_calendar()
    print("Готово: тестовий календар заповнено початковими подіями.")
