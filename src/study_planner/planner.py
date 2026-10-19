"""Агенти навчального планувальника.

Мультиагентна система:
  Аналітик     — читає дедлайни й календар, складає план. Писати НЕ може.
  Планувальник — перевіряє план за правилами і єдиний записує блоки в календар.
Для порівняння: один агент з усіма інструментами й тими самими правилами.

Запуск (з папки code):
  python planner.py "Сплануй мені підготовку до дедлайнів цього тижня"
  python planner.py "..." --single    # один агент замість двох
"""

import asyncio
import json
import os
import sys
import time
from datetime import date

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
today = date.fromisoformat(os.getenv("PLANNER_TODAY") or date.today().isoformat())
TODAY = f"{today}, {WEEKDAYS[today.weekday()]}"
SERVER = StdioServerParameters(command=sys.executable, args=["server.py"])

llm = genai.Client()  # ключ береться з GEMINI_API_KEY у файлі .env
usage = {"requests": 0, "tokens": 0}  # скільки витрачено запитів і токенів Gemini

# ---------- промпти ----------

RULES = """Правила планування:
R1. Кожен блок закінчується до дедлайну свого завдання.
R2. Блоки не перетинаються з іншими подіями в календарі.
R3. Не більше 6 годин навчальних блоків на день.
R4. Блоки тільки між 08:00 і 23:00.
R5. Кожне завдання отримує стільки годин, скільки вказано в est_hours (якщо користувач не просить інше).
Якщо бажаний час зайнятий або порушує правила — не відмовляйся, а сам обери найближчий вільний час
до дедлайну (можна розбити на кілька блоків) і поясни, що переніс.
Якщо годин не вистачає навіть так — запиши, що вміщається, і прямо скажи, чого не вистачило."""

ANALYST_PROMPT = f"""Ти — Аналітик навчального планувальника. Сьогодні {TODAY}.
Прочитай дедлайни (get_deadlines) і зайнятість календаря (get_events) і склади план навчальних блоків.
Ти НЕ записуєш у календар — це робить Планувальник.
{RULES}
Відповідь: список блоків у форматі `назва | start | end | deadline_id` (час YYYY-MM-DDTHH:MM),
а окремо — що не вмістилось і чому."""

PLANNER_PROMPT = f"""Ти — Планувальник. Сьогодні {TODAY}. Ти єдиний, хто записує в календар (create_study_block).
Ти отримуєш запит користувача і план від Аналітика. Перевір кожен блок за правилами
(за потреби подивись календар через get_events), виправ порушення і запиши блоки.
Якщо create_study_block повертає помилку — прочитай hint і спробуй інший час.
{RULES}
Наприкінці коротко звітуй: що записано, що не вмістилось і чому."""

SINGLE_PROMPT = f"""Ти — навчальний планувальник. Сьогодні {TODAY}.
Читаєш дедлайни (get_deadlines) і календар (get_events) і сам записуєш блоки (create_study_block).
Якщо create_study_block повертає помилку — прочитай hint і спробуй інший час.
{RULES}
Наприкінці коротко звітуй: що записано, що не вмістилось і чому."""

# ---------- трейсінг: кожен крок пишемо в консоль і у файл traces/*.jsonl ----------

os.makedirs("traces", exist_ok=True)
TRACE_FILE = f"traces/{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def trace(agent, kind, data):
    print(f"[{agent}] {kind}: {json.dumps(data, ensure_ascii=False)[:300]}")
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        record = {"time": time.strftime("%H:%M:%S"), "agent": agent, "kind": kind, "data": data}
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- один агент = цикл "LLM думає -> викликає інструмент -> бачить результат" ----------

async def ask_llm(contents, config):
    # Безкоштовний ключ має ліміт запитів на хвилину — якщо вперлись, чекаємо й пробуємо знову
    for attempt in range(5):
        try:
            response = await llm.aio.models.generate_content(model=MODEL, contents=contents, config=config)
            usage["requests"] += 1
            usage["tokens"] += response.usage_metadata.total_token_count or 0
            return response
        except errors.APIError as e:
            if getattr(e, "code", None) in (429, 503) and attempt < 4:
                reason = "вичерпано ваш ліміт запитів" if e.code == 429 else "модель перевантажена на боці Google"
                print(f"Gemini {e.code}: {reason}. Чекаю 30 с... ({e.message[:120]})")
                await asyncio.sleep(30)
            else:
                raise


async def run_agent(name, prompt, tool_names, message, session):
    # Агент бачить тільки дозволені йому інструменти з MCP-сервера
    tools = (await session.list_tools()).tools
    declarations = [
        types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.input_schema)
        for t in tools if t.name in tool_names
    ]
    config = types.GenerateContentConfig(
        system_instruction=prompt,
        tools=[types.Tool(function_declarations=declarations)],
    )
    contents = [types.Content(role="user", parts=[types.Part(text=message)])]
    trace(name, "запит", message)

    for _ in range(20):  # не більше 20 кроків, щоб агент не зациклився
        response = await ask_llm(contents, config)
        if not response.function_calls:
            trace(name, "відповідь", response.text)
            return response.text or ""

        contents.append(response.candidates[0].content)
        results = []
        for call in response.function_calls:
            args = dict(call.args or {})
            result = await session.call_tool(call.name, args)  # виклик через MCP
            text = result.content[0].text
            try:
                output = json.loads(text)
            except json.JSONDecodeError:
                output = text
            trace(name, "інструмент", {"name": call.name, "args": args, "result": output})
            results.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response={"result": output})))
        contents.append(types.Content(role="user", parts=results))

    return "Агент не впорався за 20 кроків"


# ---------- дві системи ----------

async def run_multi(request, session):
    plan = await run_agent("Аналітик", ANALYST_PROMPT, ["get_deadlines", "get_events"], request, session)
    message = f"Запит користувача: {request}\n\nПлан від Аналітика:\n{plan}"
    return await run_agent("Планувальник", PLANNER_PROMPT, ["get_events", "create_study_block"], message, session)


async def run_single(request, session):
    tools = ["get_deadlines", "get_events", "create_study_block"]
    return await run_agent("Один агент", SINGLE_PROMPT, tools, request, session)


async def main():
    request = sys.argv[1]
    run = run_single if "--single" in sys.argv else run_multi
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        answer = await run(request, session)
    print("\n=== Підсумок ===\n" + answer)
    print(f"\nТрейс: {TRACE_FILE}")
    print(f"Gemini: {usage['requests']} запитів, {usage['tokens']} токенів")


if __name__ == "__main__":
    asyncio.run(main())
