"""Тести MCP-сервера і парсера Moodle — без LLM, без Google (локальний календар).

Запуск:  uv run pytest -q
"""

import os
from datetime import datetime

# Тести завжди на однакових даних: локальний календар і дедлайни з фікстури.
# Задаємо до імпорту server/moodle, щоб вони підхопили ці значення.
os.environ["CALENDAR_BACKEND"] = "local"
os.environ["PLANNER_TODAY"] = "2026-10-19"
os.environ["MOODLE_ICS_URL"] = "data/deadlines.ics"

import pytest

import local_calendar
import moodle
import server

# d1 Квіз 3: до 2026-10-21T23:59 · d2 Лаба 2: до 2026-10-23T23:59 · d3 Есе: до 2026-10-24T23:59


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(local_calendar, "STORE", tmp_path / "calendar.json")
    monkeypatch.setattr(server, "PLAN_CSV", tmp_path / "study_plan.csv")
    local_calendar.reset_calendar()


def book(start, end, deadline_id="d2"):
    return server.schedule_session("Підготовка", start, end, deadline_id)


# ---------- schedule_session: запис і правила ----------

def test_valid_session_is_written_to_calendar_and_csv():
    result = book("2026-10-21T12:00", "2026-10-21T14:00")
    assert "event_id" in result
    blocks = [e for e in local_calendar.list_events("2026-10-21", "2026-10-21") if e["deadline_id"]]
    assert [(b["start"], b["end"], b["deadline_id"]) for b in blocks] == [("2026-10-21T12:00", "2026-10-21T14:00", "d2")]
    assert len(server.PLAN_CSV.read_text(encoding="utf-8").splitlines()) == 2  # заголовок + рядок


def test_r1_after_deadline():
    result = book("2026-10-24T10:00", "2026-10-24T12:00")  # d2 закривається 23-го
    assert result["error"] == "after_deadline" and result["retryable"]
    assert not local_calendar.list_events("2026-10-24", "2026-10-24")


def test_r2_time_conflict_names_the_event():
    result = book("2026-10-22T13:30", "2026-10-22T15:00")  # семінар 13:00–14:20
    assert result["error"] == "time_conflict"
    assert "Семінар" in result["message"] and "14:20" in result["hint"]


def test_r3_daily_limit():
    assert "event_id" in book("2026-10-22T08:00", "2026-10-22T12:00")
    assert "event_id" in book("2026-10-22T15:00", "2026-10-22T17:00")  # разом 6 год
    result = book("2026-10-22T18:00", "2026-10-22T19:00")
    assert result["error"] == "daily_limit" and "0 год" in result["hint"]


def test_r4_outside_hours():
    for start, end in [("2026-10-22T06:00", "2026-10-22T07:30"), ("2026-10-22T22:00", "2026-10-22T23:30")]:
        assert book(start, end)["error"] == "outside_hours"


@pytest.mark.parametrize("start,end", [
    ("not-a-time", "2026-10-21T10:00"),
    ("2026-10-21T12:00", "2026-10-21T11:00"),   # кінець раніше за початок
    ("2026-10-21T22:00", "2026-10-22T01:00"),   # через північ
])
def test_invalid_time(start, end):
    assert book(start, end)["error"] == "invalid_time"


def test_unknown_deadline_lists_known_ids():
    result = book("2026-10-21T12:00", "2026-10-21T13:00", deadline_id="nope")
    assert result["error"] == "unknown_deadline" and "d1" in result["hint"]


def test_r6_session_in_the_past_is_rejected():
    result = book("2026-10-18T10:00", "2026-10-18T11:00", deadline_id="d1")
    assert result["error"] == "in_the_past" and result["retryable"]
    assert "event_id" in book("2026-10-19T08:00", "2026-10-19T09:00", deadline_id="d1")  # сьогодні ще можна


def test_r6_uses_current_time_when_today_is_not_fixed(monkeypatch):
    monkeypatch.setattr(server, "now", lambda: datetime(2026, 10, 19, 11, 0))
    assert book("2026-10-19T10:30", "2026-10-19T11:00", deadline_id="d1")["error"] == "in_the_past"
    assert "event_id" in book("2026-10-19T14:00", "2026-10-19T15:00", deadline_id="d1")


def test_r5_task_budget_is_capped_at_12_hours():
    for start, end in [("2026-10-20T08:00", "2026-10-20T12:00"),   # 4 год
                       ("2026-10-21T12:00", "2026-10-21T18:00"),   # +6
                       ("2026-10-22T14:30", "2026-10-22T16:30")]:  # +2 = 12
        assert "event_id" in book(start, end)
    result = book("2026-10-22T17:00", "2026-10-22T18:00")
    assert result["error"] == "task_budget_exceeded" and "12" in result["hint"]
    assert "event_id" in book("2026-10-22T17:00", "2026-10-22T18:00", deadline_id="d3")  # інше завдання — окремий ліміт


# ---------- читання ----------

def test_get_deadlines_filters_by_range_and_gives_description():
    deadlines = server.get_deadlines("2026-10-19", "2026-10-25")["deadlines"]
    assert [d["id"] for d in deadlines] == ["d1", "d2", "d3"]
    assert "RAG" in deadlines[1]["description"]  # агент оцінює час за описом, тож він має дійти до нього


@pytest.mark.parametrize("a,b", [("garbage", "2026-10-25"), ("2026-10-25", "2026-10-19"), ("2026-01-01", "2028-01-01")])
def test_get_deadlines_rejects_bad_ranges(a, b):
    assert "error" in server.get_deadlines(a, b)  # не мовчки порожній список


def test_free_slots_show_only_the_future(monkeypatch):
    monkeypatch.setattr(server, "now", lambda: datetime(2026, 10, 19, 11, 0))
    days = server.get_free_slots("2026-10-18", "2026-10-19")["days"]
    assert days[0]["free"] == []                                        # учора — нічого
    assert days[1]["free"] == [{"start": "13:00", "end": "23:00"}]      # 11:20–11:40 коротше за 30 хв


def test_free_slots_start_with_a_buffer_on_a_15_minute_grid(monkeypatch):
    for now, first in [(datetime(2026, 10, 19, 16, 6), "16:30"), (datetime(2026, 10, 19, 16, 0), "16:15"),
                       (datetime(2026, 10, 19, 16, 50), "17:00"), (datetime(2026, 10, 19, 16, 20, 40), "16:30")]:
        monkeypatch.setattr(server, "now", lambda now=now: now)
        free = server.get_free_slots("2026-10-19", "2026-10-19")["days"][0]["free"]
        assert free[0]["start"] == first, now
    # сама відмова за R6 лишається суворою: запас стосується лише пропозицій
    monkeypatch.setattr(server, "now", lambda: datetime(2026, 10, 19, 16, 6))
    assert "event_id" in book("2026-10-19T16:07", "2026-10-19T17:00", deadline_id="d1")


def test_free_slots_exclude_events_and_report_budget():
    server.schedule_session("Підготовка", "2026-10-20T09:00", "2026-10-20T11:00", "d2")
    day = server.get_free_slots("2026-10-20", "2026-10-20")["days"][0]
    assert {"start": "08:00", "end": "09:00"} in day["free"]
    assert {"start": "16:00", "end": "19:00"} in day["free"] and {"start": "20:30", "end": "23:00"} in day["free"]
    assert not any(s["start"] < "11:00" and s["end"] > "09:00" for s in day["free"])  # наша сесія зайнята
    assert day["study_hours_left"] == 4


# ---------- парсер Moodle на реальному форматі експорту ----------

MOODLE_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1@learn
SUMMARY:Data Engineering Quiz opens
DTSTART:20261015T070000Z
CATEGORIES:Course A
END:VEVENT
BEGIN:VEVENT
UID:2@learn
SUMMARY:Data Engineering Quiz closes
DTSTART:20261015T111400Z
CATEGORIES:Course A
END:VEVENT
BEGIN:VEVENT
UID:3@learn
SUMMARY:Lab 6 (Databricks) is due
DESCRIPTION:Exploratory data analysis Lab\\n \\nPOINTS: 4
DTSTART:20261021T205900Z
END:VEVENT
BEGIN:VEVENT
UID:4@learn
SUMMARY:Строк Assignment 1. Build an NLP System спливає
DTSTART:20261107T215900Z
END:VEVENT
BEGIN:VEVENT
UID:5@learn
SUMMARY:Підсумковий тест доступно
DTSTART:20270505T050000Z
END:VEVENT
BEGIN:VEVENT
UID:6@learn
SUMMARY:Підсумковий тест закрито
DTSTART:20270508T080000Z
END:VEVENT
BEGIN:VEVENT
UID:7@learn
SUMMARY:Poster is due
DTSTART;VALUE=DATE:20261201
END:VEVENT
END:VCALENDAR
"""


def test_moodle_skips_opening_events_and_sorts_by_time():
    deadlines = moodle.parse_deadlines(MOODLE_SAMPLE.encode())
    assert [d["id"] for d in deadlines] == ["2@learn", "3@learn", "4@learn", "7@learn", "6@learn"]
    # «opens» і «доступно» відфільтровано, порядок за часом


def test_moodle_converts_to_kyiv_time_and_handles_all_day_events():
    due = {d["id"]: d["due"] for d in moodle.parse_deadlines(MOODLE_SAMPLE.encode())}
    assert due["3@learn"] == "2026-10-21T23:59"  # 20:59Z = 23:59 за Києвом (літній час)
    assert due["7@learn"] == "2026-12-01T23:59"  # подія на весь день -> кінець дня


def test_deadlines_can_be_listed_a_year_ahead_but_free_slots_only_a_month():
    assert "deadlines" in server.get_deadlines("2026-10-19", "2027-10-18")
    assert server.get_free_slots("2026-10-19", "2027-10-18")["error"] == "range_too_long"


# ---------- посилання на завдання в описі події ----------

LINK_SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:111@learn
SUMMARY:Quiz closes
DESCRIPTION:Пройти тут: https://learn.example/mod/quiz/view.php?id=85190.
DTSTART:20261021T205900Z
END:VEVENT
BEGIN:VEVENT
UID:222@learn
SUMMARY:Lab is due
DESCRIPTION:Без посилання
DTSTART:20261021T205900Z
END:VEVENT
BEGIN:VEVENT
UID:333@learn
SUMMARY:Essay is due
URL:https://learn.example/mod/assign/view.php?id=7
DTSTART:20261021T205900Z
END:VEVENT
END:VCALENDAR
"""


def test_direct_link_comes_from_the_url_property_or_the_description_and_falls_back_to_the_calendar_day():
    quiz, lab, essay = moodle.parse_deadlines(LINK_SAMPLE.encode(), base="https://learn.example")
    assert quiz["link"] == "https://learn.example/mod/quiz/view.php?id=85190"      # крапка наприкінці речення відкинута
    assert essay["link"] == "https://learn.example/mod/assign/view.php?id=7"       # властивість URL важливіша
    assert lab["link"] is None
    assert lab["calendar_link"].startswith("https://learn.example/calendar/view.php?view=day&time=")
    assert lab["calendar_link"].endswith("#event_222")
    assert moodle.parse_deadlines(LINK_SAMPLE.encode())[1]["calendar_link"] is None  # без адреси сайту (локальний файл) — без посилання


def test_models_never_see_the_links():
    deadline = {"id": "x", "course": "К", "title": "T", "description": "d", "due": "2026-10-21T23:59",
                "link": "https://learn.example/a", "calendar_link": "https://learn.example/b"}
    server_view = server.event_description(deadline)
    assert "https://learn.example/a" in server_view and "https://learn.example/b" not in server_view   # пряме посилання важливіше
    assert "https://learn.example/b" in server.event_description({**deadline, "link": None})


def test_get_deadlines_hides_link_fields_but_the_event_description_gets_the_link(monkeypatch):
    deadline = {"id": "d2", "course": "Генеративний ШІ", "title": "Лабораторна 2", "description": "RAG", "due": "2026-10-23T23:59",
                "link": "https://learn.example/mod/assign/view.php?id=7", "calendar_link": None}
    monkeypatch.setattr(moodle, "load_deadlines", lambda: [deadline])

    assert "link" not in server.get_deadlines("2026-10-19", "2026-10-25")["deadlines"][0]
    assert "event_id" in book("2026-10-21T12:00", "2026-10-21T14:00")
    stored = next(e for e in local_calendar._load() if e["deadline_id"] == "d2")
    assert stored["description"] == ("Підготовка до: Лабораторна 2\nКурс: Генеративний ШІ\nДедлайн: 2026-10-23 23:59\n"
                                     "Завдання в Moodle: https://learn.example/mod/assign/view.php?id=7")
    assert "description" not in local_calendar.list_events("2026-10-21", "2026-10-21")[-1]    # list_events опис не віддає
