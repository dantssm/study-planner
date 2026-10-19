"""CLI: студент → агенти → відповідь (контейнер «CLI» зі схеми C4).

Запуск (з кореня репозиторію):
  uv run python cli.py                        # розмова: домовляєтесь з Оркестратором про план
  uv run python cli.py "Сплануй тиждень"      # разовий запуск: одна відповідь і вихід
  uv run python cli.py "..." --single         # разово, але один агент замість двох
  uv run python cli.py ... --trace            # рядок на крок: хто що викликав (повні дані — у traces/*.jsonl)
  uv run python cli.py ... --verbose          # повні дані кроків у консолі

У розмові Оркестратор спершу показує план і питає один раз: у календар потрапляють лише схвалені вами
сесії (це перевіряє код, а не модель). Після відповіді видно чек того, що сервер справді записав.
Під відповіддю видно, які інструменти викликали агенти. Вийти — Ctrl+C. Кольори вимикаються, коли вивід не в терміналі
або заданий NO_COLOR.
"""

import argparse
import asyncio
import os
import re
import signal
import sys
from datetime import datetime

import agents

try:
    import readline  # noqa: F401 — стрілки й редагування рядка (з кирилицею) у input()
except ImportError:
    pass

YES = {"", "y", "yes", "т", "так"}
NO = {"n", "no", "н", "ні"}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
BOLD, DIM, RED, GREEN, YELLOW, CYAN = "1", "2", "31", "32", "33", "36"
CLEAR_LINE = "\r\033[K" if COLOR else ""


def paint(text, *codes):
    return f"\033[{';'.join(codes)}m{text}\033[0m" if COLOR else text


def ask(prompt_text):
    """Рядок введення. Колір у підказці обгорнутий у \\001…\\002, щоб readline правильно рахував ширину."""
    if COLOR:
        prompt_text = f"\001\033[{BOLD};{CYAN}m\002{prompt_text}\001\033[0m\002"
    return input(prompt_text)


def render(text):
    """Легкий Markdown: **жирний**, `код`, заголовки, списки. Без кольорів текст лишається як є."""
    if not COLOR:
        return text
    lines = []
    for line in text.splitlines():
        body = line.lstrip()
        indent = line[:len(line) - len(body)]
        if body.startswith("#"):
            line = paint(body.lstrip("#").strip(), BOLD)
        elif body[:2] in ("- ", "* "):
            line = f"{indent}• {body[2:]}"
        line = re.sub(r"\*\*(.+?)\*\*", lambda m: paint(m.group(1), BOLD), line)
        line = re.sub(r"`([^`]+)`", lambda m: paint(m.group(1), CYAN), line)
        lines.append(line)
    return "\n".join(lines)


def session_row(start, end, title):
    day = datetime.fromisoformat(start)
    hours = (datetime.fromisoformat(end) - day).total_seconds() / 3600
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}  {start[11:]}–{end[11:]}  {hours:>4g} год  {title}", hours


def print_sessions(sessions, heading, footer_label="Разом", color=CYAN):
    """Сесії таблицею з лівою рамкою: ╭─ заголовок, │ рядки, ╰─ підсумок. Повертає суму годин."""
    rows = [session_row(s["start"], s["end"], s["title"]) for s in sorted(sessions, key=lambda s: s["start"])]
    total = sum(hours for _, hours in rows)
    print(paint("  ╭─ ", color) + paint(heading, BOLD))
    for row, _ in rows:
        print(paint("  │ ", color) + row)
    print(paint("  ╰─ ", color) + f"{footer_label} {total:g} год")
    return total


def print_tools(calls):
    """Які інструменти викликали агенти: «агент: інструмент ×кількість (⚠ помилки)»."""
    if not calls:
        return
    by_agent = {}
    for agent, tool, failed in calls:
        stats = by_agent.setdefault(agent, {}).setdefault(tool, [0, 0])
        stats[0] += 1
        stats[1] += failed
    print(paint("  ⚙ Інструменти", DIM))
    for agent, tools in by_agent.items():
        names = ", ".join(tool + (f" ×{n}" if n > 1 else "") + (f" (⚠{bad})" if bad else "")
                          for tool, (n, bad) in tools.items())
        print(paint(f"    {agent}: {names}", DIM))


def print_usage(trace_file):
    u = agents.usage
    print(paint(f"\n  Трейс: {trace_file}\n  LLM ({agents.MODEL}): {u['requests']} запитів, {u['tokens']} токенів, "
                f"{u['tool_calls']} викликів інструментів ({u['tool_errors']} з помилкою)", DIM))


def print_summary():
    """Підсумок за відповідями сервера, а не за словами моделі."""
    print()
    if not agents.written:
        print(paint("  Підсумок: у календар нічого не записано.", DIM))
        return
    print_sessions(agents.written, "Підсумок: записано в календар (за даними сервера)", color=GREEN)


async def ask_plan(blocks, note):
    """Показує план і питає схвалення: (схвалено, побажання). Єдине місце, де людина дозволяє запис."""
    print(CLEAR_LINE)
    print_sessions(blocks, "Пропонований план", color=YELLOW)
    if note:
        print(paint(f"  {note}", DIM))
    print(paint("\n  Записати цей план?", BOLD) + paint("  Enter — так · n — ні · або напишіть, що змінити", DIM))
    try:
        answer = ask(">> ").strip()
    except EOFError:
        return False, "користувач недоступний"
    if answer.lower() in YES:
        return True, ""
    return False, "" if answer.lower() in NO else answer


async def chat(session):
    """Розмова триває до Ctrl+C, історія зберігається між репліками."""
    print(paint("\n  Study planner", BOLD, CYAN) + paint("  ·  помічник із планування навчання", DIM))
    print(paint("  Напишіть, що потрібно. Вийти — Ctrl+C.", DIM))
    history, approval = [], agents.PlanApproval(ask_plan)
    try:
        while True:
            print()
            text = ask(">> ").strip()
            if not text:
                continue
            already, calls_before = len(agents.written), len(agents.tool_log)
            if agents.console_mode == "off":
                print(paint("  думаю…", DIM), end="", flush=True)  # інакше довга відповідь схожа на зависання
            try:
                answer = await agents.chat_turn(session, history, text, approval)
            except agents.LLMUnavailable as e:
                print(CLEAR_LINE + paint(f"\n  ⛔ {e}", RED))
                break
            print(CLEAR_LINE + "\n" + paint("● Помічник", BOLD, GREEN))
            print("\n".join("  " + line for line in render(answer).splitlines()))
            print()
            print_tools(agents.tool_log[calls_before:])
            if new := agents.written[already:]:  # чек за даними сервера, а не зі слів моделі
                print()
                print_sessions(new, "✓ Записано в календар", color=GREEN)
    except (KeyboardInterrupt, EOFError):  # Ctrl+C або Ctrl+D — звичайний вихід
        print(CLEAR_LINE + paint("\n  До побачення!", DIM))
    finally:
        print_summary()


async def main():
    parser = argparse.ArgumentParser(description="Навчальний планувальник")
    parser.add_argument("request", nargs="?", help="разовий запит; без нього — розмова")
    parser.add_argument("--single", action="store_true", help="разовий запуск одним агентом замість двох")
    parser.add_argument("--trace", action="store_true", help="друкувати рядок на кожен крок агентів")
    parser.add_argument("--verbose", action="store_true", help="друкувати в консоль повні дані кроків")
    args = parser.parse_args()
    if args.single and not args.request:
        parser.error("--single працює лише з разовим запитом")
    agents.console_mode = "full" if args.verbose else "compact" if args.trace else "off"
    agents.require_key()  # до запуску MCP-сервера, щоб помилка була короткою

    trace_file = agents.start_trace("chat" if not args.request else "single" if args.single else "multi")
    async with agents.mcp_session() as session:
        if not args.request:
            await chat(session)
        else:
            run = agents.run_single if args.single else agents.run_multi
            try:
                print("\n" + paint("● Підсумок агента", BOLD, GREEN) + "\n" + render(await run(args.request, session)))
                print()
                print_tools(agents.tool_log)
            except agents.LLMUnavailable as e:
                print(paint(f"\n  ⛔ {e}", RED))
            print_summary()
    print_usage(trace_file)


def raise_interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # Власний обробник Ctrl+C: інакше asyncio.run лише скасовує задачу, а блокований input() чекав би Enter
    signal.signal(signal.SIGINT, raise_interrupt)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C під час очікування моделі (в input() його ловить chat)
        print(paint("\n  До побачення!", DIM))
