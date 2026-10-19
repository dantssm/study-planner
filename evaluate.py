"""Оцінка точності: 5 тестових запитів через мультиагентну систему і через одного агента.

Перед кожним тестом календар скидається до початкового стану. Тест пройдено, якщо в підсумковому
календарі виконані всі правила, потрібні завдання отримали свої години в потрібні дні, зайвого не записано.
Окрім результату (outcome), для кожного прогону рахуємо: причину зупинки (успіх чи який саме вид невдачі),
процес за трейсом (повторні виклики, відмови в доступі, збої Планувальника), ефективність (запити, токени,
секунди) і ціну на один пройдений тест — як на слайдах про оцінювання агентів.

Запуск (з кореня репозиторію):
  uv run python evaluate.py                  # усі 5 тестів, обидві системи, локальний календар
  uv run python evaluate.py multi            # тільки два агенти
  uv run python evaluate.py single 4 5       # тільки один агент, тести 4 і 5
  uv run python evaluate.py --google         # на справжньому тестовому Google-календарі
Результати накопичуються в results.json, таблиця — у консолі і в results.md
"""

import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

# Тести завжди на однакових даних: дедлайни з локального .ics і фіксоване "сьогодні".
# Задаємо до імпорту agents/moodle/calendars, щоб вони підхопили ці значення.
TODAY = "2026-10-19"
os.environ["MOODLE_ICS_URL"] = "data/deadlines.ics"
os.environ["PLANNER_TODAY"] = TODAY
os.environ["CALENDAR_BACKEND"] = "google" if "--google" in sys.argv else "local"

import agents
import calendars
import moodle
import server

# Суддя дивиться на всі події від «сьогодні» до кінця року, щоб не пропустити сесії поза тижнем тестів
CHECK_FROM, CHECK_TO = TODAY, "2026-12-31"

# Скільки годин агент має право заплановувати на завдання, якщо користувач не назвав число:
# оцінку робить сам агент за описом, тест лише перевіряє, що вона розумна (не 0 і не абсурд)
SANE_HOURS = {"d1": (0.5, 3), "d2": (2, 8), "d3": (1.5, 5)}

RUNNERS = {"multi": agents.run_multi, "single": agents.run_single}
SYSTEM_LABELS = {"multi": "Два агенти", "single": "Один агент"}

TESTS = [
    {"id": 1, "name": "Ліміт 2 год на день, без вт і ср", "request": "Сплануй підготовку до всіх дедлайнів цього тижня. Вчитися можу не більше 2 годин на день "
                         "і не вчуся у вівторок та середу.",
     "hours": {"d1": (0, 3), "d2": (0, 8), "d3": (0, 5)},  # місця на все не вистачає (Пн, Чт, Пт, Сб = 8 год): лише ≤ 2 год на день,
     "free_days": ["2026-10-20", "2026-10-21"],            # тож треба вмістити що можна й чесно сказати, чого не вистачило
     "max_per_day": 2, "min_total": 6},
    {"id": 2, "name": "Середа зайнята", "request": "Сплануй підготовку до дедлайнів цього тижня. У середу 21 жовтня я зайнята весь день.",
     "hours": SANE_HOURS, "free_days": ["2026-10-21"]},
    {"id": 3, "name": "Лаба 2 після дедлайну", "request": "Постав 2 години на Лабораторну 2 з Генеративного ШІ в суботу 24 жовтня о 10:00.",
     "hours": {"d2": (2, 2)}, "free_days": []},  # субота пізніше дедлайну -> after_deadline, треба перенести
    {"id": 4, "name": "Конфлікт із семінаром", "request": "Додай 3 години на есе з Баз даних у четвер 22 жовтня з 12:00.",
     "hours": {"d3": (3, 3)}, "on_day": {"2026-10-22": 3},  # о 13:00 семінар -> time_conflict, треба обійти
     "free_days": []},
    {"id": 5, "name": "12 год в один день (ліміт 6)", "request": "Постав по 4 години на кожне завдання цього тижня, все у вівторок 20 жовтня.",
     "hours": {"d1": (0, 4), "d2": (0, 4), "d3": (0, 4)}, "on_day": {"2026-10-20": 6},  # 12 годин порушує R3:
     "free_days": []},                                                                    # вівторок заповнити до ліміту
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

        if b["start"] < f"{TODAY}T00:00":
            problems.append(f"R6: «{b['title']}» у минулому")
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

    for deadline_id, (low, high) in test["hours"].items():
        planned = hours_per_deadline.get(deadline_id, 0)
        if not low <= planned <= high:
            problems.append(f"R5: для {deadline_id} заплановано {planned:g} год, очікували {low:g}–{high:g}")

    if test["hours"]:  # завдання, про які не просили, чіпати не можна
        for deadline_id in hours_per_deadline.keys() - test["hours"].keys():
            problems.append(f"заплановано {hours_per_deadline[deadline_id]:g} год для {deadline_id}, про яке не просили")

    if limit := test.get("max_per_day"):  # обмеження користувача, якого сервер не знає
        for day, hours in hours_per_day.items():
            if hours > limit:
                problems.append(f"користувач просив не більше {limit:g} год на день, а {day} заплановано {hours:.3g}")
    if (minimum := test.get("min_total")) and sum(hours_per_deadline.values()) < minimum:  # не здаватися, коли місце є
        problems.append(f"записано лише {sum(hours_per_deadline.values()):g} год, а в дозволені дні вміщається щонайменше {minimum:g}")

    for day, wanted in test.get("on_day", {}).items():  # користувач просив саме цей день
        if hours_per_day.get(day, 0) < wanted:
            problems.append(f"на {day} заплановано {hours_per_day.get(day, 0):g} год, а просили щонайменше {wanted:g}")

    if not blocks:
        problems.append("нічого не записано")

    return problems


RESULTS = Path("results.json")


def load_results():
    return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}


def save_results(results):
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- причина зупинки, процес і ціна ----------

HARD_RULES = ("R1", "R2", "R3", "R4", "R6")  # їх сервер не пропустить: порушення означало б помилку в самому сервері
DENIED = {"permission_denied", "repeated_call", "not_approved"}
PLANNER_ERRORS = {"planner_failed", "planner_invalid_plan", "planner_budget_exceeded"}


def stop_reason(problems, stops):
    """Чому прогін закінчився так: успіх і різні види невдач окремо (слайд «split by stop reason»)."""
    if not problems:
        return "success"
    if stops:
        return "agent_stopped"
    if "нічого не записано" in problems:
        return "nothing_written"
    if any(p.startswith(HARD_RULES) for p in problems):
        return "rules_violated"
    return "wrong_result"


def analyze_trace(path):
    """Процес за трейсом (слайд «Process»): повторні виклики, відмови в доступі, виклики Планувальника, помилки за кодами."""
    analysis = {"duplicate_calls": 0, "denied": 0, "plan_study_calls": 0, "errors": {}}
    if not Path(path).exists():
        return analysis
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["kind"] != "інструмент":
            continue
        data = record["data"]
        key = (record["agent"], data["name"], json.dumps(data["args"], sort_keys=True, ensure_ascii=False))
        analysis["duplicate_calls"] += key in seen
        seen.add(key)
        analysis["plan_study_calls"] += data["name"] == "plan_study"
        code = data["result"].get("error") if isinstance(data["result"], dict) else None
        if code:
            analysis["errors"][code] = analysis["errors"].get(code, 0) + 1
            analysis["denied"] += code in DENIED
    return analysis


def process_issues(run):
    """Що в процесі насторожує. Порожній список — процес чистий."""
    process, issues = run.get("process", {}), []
    if process.get("duplicate_calls"):
        issues.append(f"{process['duplicate_calls']} повторних викликів")
    if process.get("denied"):
        issues.append(f"{process['denied']} відмов у доступі")
    if PLANNER_ERRORS & set(process.get("errors", {})):
        issues.append("збій Планувальника")
    if run.get("stops"):
        issues.append(f"агент здавався {run['stops']} р.")
    return issues


def cost(run):
    return f"{run['requests']} зап. · {run['tokens'] / 1000:.1f}к ток. · {run.get('seconds', 0):.0f} с"


def cell(run):
    if run is None:
        return "не запускали"
    status = "ok" if not run["problems"] else f"fail ({run.get('stop_reason', '?')})"
    return f"{status} · {cost(run)}"


def summary_row(runs, label):
    """Рядок підсумку: скільки пройдено, причини невдач, ціна одного пройденого тесту (усі витрати ÷ успіхи)."""
    passed = sum(not run["problems"] for run in runs)
    failed = Counter(r.get("stop_reason", "?") for r in runs if r["problems"])
    reasons = ", ".join(f"{reason} {n}" for reason, n in failed.most_common()) or "—"
    total = {key: sum(r.get(key, 0) for r in runs) for key in ("tokens", "requests", "seconds")}
    per_pass = ([f"{total['tokens'] / passed / 1000:.1f}к", f"{total['requests'] / passed:.1f}",
                 f"{total['seconds'] / passed:.0f} с"] if passed else ["—"] * 3)
    return f"| {label} | {passed}/{len(runs)} | {reasons} | " + " | ".join(per_pass) + " |"


def details(results):
    """Що пішло не так: порушення за тестами й підозри в процесі (повтори, відмови в доступі, збої)."""
    failures, warnings = [], []
    for test in TESTS:
        for system, label in SYSTEM_LABELS.items():
            run = results.get(f"{test['id']}-{system}")
            if run is None:
                continue
            if run["problems"]:
                failures.append(f"- Тест {test['id']}, {label} ({run.get('stop_reason', '?')}): " + "; ".join(run["problems"]))
            if issues := process_issues(run):
                warnings.append(f"- Тест {test['id']}, {label}: " + ", ".join(issues))
    return failures, warnings


def save_table(results):
    """Таблиця з усіх запусків у results.json: у консоль і в results.md."""
    lines = ["| # | Тест | Два агенти | Один агент |", "|---|---|---|---|"]
    for test in TESTS:
        multi, single = (cell(results.get(f"{test['id']}-{system}")) for system in SYSTEM_LABELS)
        lines.append(f"| {test['id']} | {test['name']} | {multi} | {single} |")

    lines += ["", "| | Пройдено | Причини невдач | Токенів на успіх | Запитів на успіх | Час на успіх |",
              "|---|---|---|---|---|---|"]
    for system, label in SYSTEM_LABELS.items():
        runs = [results[k] for t in TESTS if (k := f"{t['id']}-{system}") in results]
        lines.append(summary_row(runs, label))

    failures, warnings = details(results)
    for title, items in (("Невдачі", failures), ("Підозри в процесі", warnings)):
        if items:
            lines += ["", f"{title}:", *items]

    setups = sorted({(run.get("model", "?"), run.get("calendar", "?")) for run in results.values()})
    header = "; ".join(f"Модель: {model} · календар: {calendar}" for model, calendar in setups)
    if len(setups) > 1:
        header += "\n\nУвага: у results.json змішані прогони різних моделей чи календарів, порівнювати їх не можна."
    text = f"{header}\n\n" + "\n".join(lines) + "\n"
    print("\n" + text)
    Path("results.md").write_text(text, encoding="utf-8")


async def run_test(test, system, session):
    """Один прогін: чистий календар, система відпрацьовує запит, суддя дивиться на підсумковий календар."""
    calendars.reset_calendar()
    server.PLAN_CSV.unlink(missing_ok=True)
    trace_file = agents.start_trace(f"test{test['id']}-{system}")
    before = dict(agents.usage)
    started = time.perf_counter()
    await RUNNERS[system](test["request"], session)
    seconds = round(time.perf_counter() - started, 1)
    used = {k: agents.usage[k] - before[k] for k in before}
    problems = check(test, calendars.list_events(CHECK_FROM, CHECK_TO))
    return {"problems": problems, "stop_reason": stop_reason(problems, used["stops"]), "seconds": seconds,
            "process": analyze_trace(trace_file), "trace": str(trace_file),
            "model": agents.MODEL, "calendar": calendars.BACKEND, **used}


async def main():
    systems = [s for s in RUNNERS if s in sys.argv] or list(RUNNERS)
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()] or [t["id"] for t in TESTS]

    agents.require_key()
    results = load_results()
    jobs = [(system, test) for system in systems for test in TESTS if test["id"] in ids]
    async with agents.mcp_session() as session:
        for system, test in jobs:
            print(f"\n########## Тест {test['id']} · {system} ##########")
            try:
                results[f"{test['id']}-{system}"] = await run_test(test, system, session)
            except agents.LLMUnavailable as e:  # решту можна дозапустити пізніше: зроблене вже збережено
                print(f"\n⛔ {e}\nЗупиняюсь; уже зроблене збережено. Дозапустіть решту пізніше, напр. "
                      f"`uv run python evaluate.py {system} {test['id']}` і далі.")
                break
            save_results(results)  # після кожного тесту
    save_table(results)


if __name__ == "__main__":
    asyncio.run(main())
