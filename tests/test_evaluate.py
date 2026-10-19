"""Тести «судді» з evaluate.py: він має ловити і порушення правил, і недотримання запиту користувача.

Запуск:  uv run pytest -q
"""

import json
import sys

sys.argv = ["pytest"]  # evaluate читає --google з аргументів; тут завжди локальний календар

import evaluate

TESTS = {t["id"]: t for t in evaluate.TESTS}


def session(start, end, deadline_id, title="сесія"):
    return {"id": f"{deadline_id}{start}", "title": title, "start": start, "end": end, "deadline_id": deadline_id}


def pair(start, end):  # пара чи особиста подія без deadline_id
    return {"id": start, "title": "пара", "start": start, "end": end, "deadline_id": None}


GOOD = {
    1: [session("2026-10-19T08:00", "2026-10-19T10:00", "d1"), session("2026-10-22T08:00", "2026-10-22T10:00", "d2"),
        session("2026-10-23T14:00", "2026-10-23T16:00", "d2"), session("2026-10-24T10:00", "2026-10-24T12:00", "d3")],
    2: [session("2026-10-20T08:00", "2026-10-20T09:00", "d1"), session("2026-10-22T08:00", "2026-10-22T12:00", "d2"),
        session("2026-10-20T16:00", "2026-10-20T19:00", "d3")],
    3: [session("2026-10-23T14:00", "2026-10-23T16:00", "d2")],
    4: [session("2026-10-22T14:30", "2026-10-22T17:30", "d3")],
    5: [session("2026-10-20T08:00", "2026-10-20T12:00", "d1"), session("2026-10-20T12:00", "2026-10-20T14:00", "d2")],
}


def problems_for(test_id, sessions, others=()):
    return evaluate.check(TESTS[test_id], list(others) + sessions)


def test_correct_results_pass_every_test():
    for test_id, sessions in GOOD.items():
        assert problems_for(test_id, sessions) == [], test_id


def test_nothing_written_fails():
    assert "нічого не записано" in problems_for(4, [])


def test_wrong_day_fails_even_though_rules_are_kept():
    monday = [session("2026-10-19T14:00", "2026-10-19T17:00", "d3")]
    assert any("2026-10-22" in p for p in problems_for(4, monday))


def test_tuesday_must_be_filled_when_user_asks_for_it():
    spread = [session("2026-10-20T08:00", "2026-10-20T10:00", "d1"), session("2026-10-21T12:00", "2026-10-21T16:00", "d2")]
    assert any("2026-10-20" in p for p in problems_for(5, spread))


def test_unrequested_task_is_flagged():
    extra = GOOD[3] + [session("2026-10-22T08:00", "2026-10-22T10:00", "d4")]
    assert any("d4" in p for p in problems_for(3, extra))


def test_hours_outside_the_sane_range_fail():
    too_much = [session("2026-10-20T08:00", "2026-10-20T14:00", "d1")] + GOOD[2][1:]      # 6 год на квіз
    assert any("d1" in p and "R5" in p for p in problems_for(2, too_much))


def test_user_daily_cap_is_enforced_even_though_the_server_allows_more():
    over_cap = GOOD[1][:1] + [session("2026-10-22T08:00", "2026-10-22T12:00", "d2")] + GOOD[1][3:]   # 4 год у четвер
    assert any("не більше 2" in p and "2026-10-22" in p for p in problems_for(1, over_cap))


def test_giving_up_early_fails_when_the_allowed_days_have_room():
    assert any("записано лише" in p for p in problems_for(1, GOOD[1][:1]))


def test_days_the_user_does_not_study_are_respected():
    on_tuesday = [session("2026-10-20T08:00", "2026-10-20T10:00", "d1")] + GOOD[1][1:]
    assert any("зайнятий" in p for p in problems_for(1, on_tuesday))


def test_busy_day_is_respected():
    on_busy_day = [session("2026-10-21T12:00", "2026-10-21T13:00", "d1")] + GOOD[2][1:]
    assert any("зайнятий" in p for p in problems_for(2, on_busy_day))


def test_rule_violations_are_caught():
    assert any("R1" in p for p in problems_for(3, [session("2026-10-24T10:00", "2026-10-24T12:00", "d2")]))
    assert any("R4" in p for p in problems_for(3, [session("2026-10-23T06:00", "2026-10-23T08:00", "d2")]))
    assert any("R6" in p for p in problems_for(3, [session("2026-10-18T10:00", "2026-10-18T12:00", "d2")]))
    assert any("R2" in p for p in problems_for(4, GOOD[4], others=[pair("2026-10-22T15:00", "2026-10-22T16:00")]))
    day_of_seven = [session("2026-10-22T08:00", "2026-10-22T12:00", "d3"), session("2026-10-22T15:00", "2026-10-22T18:00", "d3")]
    assert any("R3" in p for p in problems_for(4, day_of_seven))


def test_sessions_after_the_test_week_are_still_judged():
    assert evaluate.CHECK_TO >= "2026-12-31"
    late = GOOD[3] + [session("2026-11-02T10:00", "2026-11-02T12:00", "d5")]
    assert any("d5" in p for p in problems_for(3, late))


# ---------- причина зупинки, процес і ціна на пройдений тест ----------

def test_stop_reason_separates_success_from_each_kind_of_failure():
    assert evaluate.stop_reason([], 0) == "success"
    assert evaluate.stop_reason([], 2) == "success"                                      # агент здавався, але результат правильний
    assert evaluate.stop_reason(["R5: для d2 заплановано 0 год"], 1) == "agent_stopped"
    assert evaluate.stop_reason(["нічого не записано"], 0) == "nothing_written"
    assert evaluate.stop_reason(["R2: «а» перетинається з «б»"], 0) == "rules_violated"
    assert evaluate.stop_reason(["R5: для d2 заплановано 9 год"], 0) == "wrong_result"   # правила дотримані, а запит ні
    assert evaluate.stop_reason(["на 2026-10-22 заплановано 0 год"], 0) == "wrong_result"


def trace_line(agent, name, args, result, kind="інструмент"):
    return json.dumps({"agent": agent, "kind": kind, "data": {"name": name, "args": args, "result": result}}, ensure_ascii=False)


def test_trace_analysis_counts_repeats_denials_planner_calls_and_error_codes(tmp_path):
    week = {"date_from": "2026-10-19", "date_to": "2026-10-25"}
    lines = [
        trace_line("Оркестратор", "plan_study", {"request": "x"}, {"plan": {}}),
        trace_line("Планувальник", "get_free_slots", week, {"days": []}),
        trace_line("Планувальник", "get_free_slots", week, {"days": []}),                  # повтор того самого виклику
        trace_line("Планувальник", "schedule_session", {}, {"error": "permission_denied"}),
        trace_line("Оркестратор", "schedule_session", {"s": 1}, {"error": "time_conflict"}),
        trace_line("Оркестратор", "schedule_session", {"s": 2}, {"event_id": "e"}),
        trace_line("Оркестратор", "x", {}, "модель", kind="модель"),                        # не виклик інструмента — ігнорується
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")

    assert evaluate.analyze_trace(path) == {"duplicate_calls": 1, "denied": 1, "plan_study_calls": 1,
                                            "errors": {"permission_denied": 1, "time_conflict": 1}}
    assert evaluate.analyze_trace(tmp_path / "missing.jsonl")["duplicate_calls"] == 0     # файлу нема — процес порожній


def test_process_issues_lists_only_what_is_suspicious():
    clean = {"process": {"duplicate_calls": 0, "denied": 0, "errors": {"time_conflict": 1}}, "stops": 0}
    assert evaluate.process_issues(clean) == []                                           # помилка сервера з виходом із неї — це норма
    messy = {"process": {"duplicate_calls": 2, "denied": 1, "errors": {"planner_failed": 1}}, "stops": 1}
    assert evaluate.process_issues(messy) == ["2 повторних викликів", "1 відмов у доступі", "збій Планувальника", "агент здавався 1 р."]


def run_record(problems, reason, tokens, requests, seconds, **extra):
    return {"problems": problems, "stop_reason": reason, "tokens": tokens, "requests": requests, "seconds": seconds,
            "tool_calls": 5, "tool_errors": 0, "stops": 0, "model": "m", "calendar": "local", **extra}


def test_table_reports_reasons_process_and_cost_per_passed_test(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    results = {
        "1-multi": run_record([], "success", 30000, 10, 60.0, process={"duplicate_calls": 0, "denied": 0, "errors": {}}),
        "2-multi": run_record([], "success", 20000, 8, 40.0, process={"duplicate_calls": 1, "denied": 0, "errors": {}}),
        "3-multi": run_record(["R5: для d2 заплановано 9 год, очікували 2–2"], "wrong_result", 10000, 6, 20.0),
        "1-single": run_record(["нічого не записано"], "nothing_written", 5000, 3, 10.0),
    }
    evaluate.save_table(results)

    table = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert "ok · 10 зап. · 30.0к ток. · 60 с" in table
    assert "fail (wrong_result)" in table
    assert "- Тест 3, Два агенти (wrong_result): R5: для d2 заплановано 9 год" in table
    assert "- Тест 2, Два агенти: 1 повторних викликів" in table
    # два агенти: 2 успіхи; усі витрати (60к токенів, 24 запити, 120 с) діляться на 2 успіхи
    assert "| Два агенти | 2/3 | wrong_result 1 | 30.0к | 12.0 | 60 с |" in table
    assert "| Один агент | 0/1 | nothing_written 1 | — | — | — |" in table
