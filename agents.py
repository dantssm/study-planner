"""Агенти навчального планувальника (контейнер «Agents» зі схеми C4).

  Оркестратор   — розуміє запит, просить план у Планувальника (інструмент plan_study),
                  єдиний записує сесії в календар.
  Планувальник  — читає дедлайни й вільні слоти, оцінює години, складає план. Писати не може.
  Один агент    — для порівняння: усі інструменти й ті самі правила в одного агента.

Усі виклики інструментів ідуть через MCP-клієнт до server.py. LLM — OpenRouter через клієнт `openai`
(API сумісний); модель задається в .env. Промпти — у prompts.py.
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import openai
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from prompts import (
    MAX_PLANNER_CALLS,
    ORCHESTRATOR_CHAT_PROMPT,
    ORCHESTRATOR_PROMPT,
    PLANNER_PROMPT,
    SINGLE_PROMPT,
    today,
)

load_dotenv()
BASE = Path(__file__).parent
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = os.getenv("LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
API_KEY = os.getenv("OPENROUTER_API_KEY")
TEMPERATURE = 0        # менше розкиду між прогонами
MAX_STEPS = 20         # кроків на агента, щоб не зациклився
MAX_SAME_CALL = 2      # скільки разів поспіль можна повторити той самий виклик
MAX_EMPTY_REPLIES = 2  # порожніх відповідей моделі (без тексту й викликів), які терпимо підряд
LLM_ATTEMPTS = 5
HOURS_TOLERANCE = 0.25  # на скільки години сесій можуть відрізнятись від оцінки

WRITE_TOOLS = {"schedule_session"}
READ_TOOLS = ["get_deadlines", "get_free_slots"]
ORCHESTRATOR_TOOLS = ["schedule_session"]

# Серверу передаємо лише потрібні йому змінні — ключ LLM йому ні до чого
SERVER_ENV = ["MOODLE_ICS_URL", "CALENDAR_BACKEND", "GOOGLE_CALENDAR_ID", "PLANNER_TODAY"]
SERVER = StdioServerParameters(
    command=sys.executable, args=[str(BASE / "server.py")],
    env={k: os.environ[k] for k in SERVER_ENV if k in os.environ},
)

usage = {"requests": 0, "tokens": 0, "tool_calls": 0, "tool_errors": 0, "stops": 0}  # stops — скільки разів агент здався
written = []  # сесії, які сервер підтвердив за цей запуск: правда, на відміну від звіту моделі
tool_log = []  # (агент, інструмент, чи була помилка) для кожного виклику за запуск: CLI показує його користувачу


# ---------- трейсінг ----------
# Кожен крок іде в traces/*.jsonl (один файл і один trace_id на запуск) і, залежно від console_mode,
# у консоль: "compact" — рядок на крок, "full" — повні дані, "off" — нічого (так за замовчуванням у cli.py).

TRACES = BASE / "traces"
trace_state = {"id": "-", "file": None}
console_mode = "compact"


def start_trace(label="run"):
    TRACES.mkdir(exist_ok=True)
    written.clear()
    tool_log.clear()
    trace_state["id"] = uuid.uuid4().hex[:8]
    trace_state["file"] = TRACES / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{trace_state['id']}.jsonl"
    return trace_state["file"]


def console_line(agent, kind, data, extra):
    if kind == "запит":
        return f"[{agent}] ▶ {str(data)[:90]}"
    if kind == "модель":
        calls = ", ".join(data["tool_calls"]) or "відповідь"
        return f"[{agent}] крок {extra['step']}: {extra['tokens']} ток., {extra['latency_ms'] / 1000:.1f} с → {calls}"
    if kind == "інструмент":
        result = data["result"]
        status = f"помилка {result['error']}" if isinstance(result, dict) and "error" in result else "ok"
        args = data["args"] or {}
        if data["name"] == "schedule_session" and {"start", "end", "title"} <= args.keys():
            brief = f"{args['start'].replace('T', ' ')}–{str(args['end'])[11:]} «{args['title']}»"
        else:
            brief = json.dumps(args, ensure_ascii=False)
            brief = brief[:70] + ("…" if len(brief) > 70 else "")
        return f"[{agent}]   {data['name']} {brief} → {status}"
    if kind == "стоп":
        return f"[{agent}] ⛔ {data}"
    return None  # «відповідь» друкується наприкінці як підсумок


def trace(agent, kind, data, **extra):
    if trace_state["file"] is None:
        start_trace()
    if console_mode == "full":
        print(f"[{agent}] {kind}: {json.dumps(data, ensure_ascii=False)[:300]}")
    elif console_mode == "compact" and (line := console_line(agent, kind, data, extra)):
        print(line)
    record = {"time": time.strftime("%H:%M:%S"), "trace_id": trace_state["id"], "agent": agent,
              "kind": kind, **extra, "data": data}
    with open(trace_state["file"], "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- LLM ----------

class AgentStopped(Exception):
    """Агент вичерпав ліміт кроків чи кілька разів поспіль мовчав і не дав відповіді."""


class LLMUnavailable(Exception):
    """Модель недоступна надовго (добовий ліміт або збій після всіх повторів): чекати чи повторювати марно."""


class EmptyResponse(Exception):
    """Безкоштовні моделі інколи віддають 200 без жодного варіанта відповіді — просто пробуємо ще раз."""


llm = None  # створюється при першому запиті, щоб імпорт працював і без ключа


def require_key():
    if not API_KEY:
        sys.exit("Немає ключа LLM: впишіть OPENROUTER_API_KEY у .env (https://openrouter.ai/keys)")


def get_llm():
    global llm
    if llm is None:
        require_key()
        llm = openai.AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY, max_retries=2)
    return llm


def daily_limit_message(error):
    reset = re.search(r"X-RateLimit-Reset'?: *'?(\d+)", str(error))
    when = ""
    if reset:
        moment = datetime.fromtimestamp(int(reset.group(1)) / 1000, ZoneInfo("Europe/Kyiv"))
        when = f" Ліміт скинеться {moment:%d.%m о %H:%M} за Києвом."
    return ("Вичерпано добовий ліміт безкоштовних запитів до моделі на OpenRouter." + when +
            " Можна почекати, змінити модель (LLM_MODEL у .env) або поповнити кредити на OpenRouter.")


async def ask_llm(messages, tools):
    """Один запит до моделі. Безкоштовні моделі мають ліміти й інколи збоять — чекаємо й пробуємо знову."""
    for attempt in range(1, LLM_ATTEMPTS + 1):
        try:
            response = await get_llm().chat.completions.create(model=MODEL, messages=messages, tools=tools,
                                                                  temperature=TEMPERATURE)
            if not response.choices:
                raise EmptyResponse("модель повернула порожню відповідь")
            usage["requests"] += 1
            usage["tokens"] += response.usage.total_tokens if response.usage else 0
            return response
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError, EmptyResponse) as e:
            rate_limited = isinstance(e, openai.RateLimitError)
            if rate_limited and "per-day" in str(e):
                raise LLMUnavailable(daily_limit_message(e)) from e  # добовий ліміт: чекати 20 с марно
            if attempt == LLM_ATTEMPTS:
                raise LLMUnavailable(f"Модель не відповідає після кількох спроб ({type(e).__name__}: {str(e)[:150]})") from e
            pause = 20 if rate_limited else 5
            print(f"LLM: {type(e).__name__} — чекаю {pause} с... ({str(e)[:120]})")
            await asyncio.sleep(pause)


# ---------- агент: цикл «модель думає → викликає інструмент → бачить результат» ----------

def tool_result(result):
    """Відповідь MCP-інструмента → dict, який побачить модель."""
    text = result.content[0].text if result.content else ""
    if result.is_error:  # збій самого сервера, а не наша бізнес-помилка
        return {"error": "tool_failed", "message": text, "retryable": False}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"result": text}


def function_spec(name, description, parameters):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


async def available_tools(session, tool_names, local_tools):
    """Інструменти в форматі моделі: дозволені з MCP-сервера плюс локальні."""
    mcp_tools = (await session.list_tools()).tools
    return ([function_spec(t.name, t.description, t.input_schema) for t in mcp_tools if t.name in tool_names] +
            [function_spec(name, t["description"], t["parameters"]) for name, t in local_tools.items()])


def refusal(name, args, repeats, allowed):
    """Відмова ще до виконання виклику (None — можна). Право виклику перевіряє клієнт, а не промпт."""
    if args is None:
        return {"error": "invalid_arguments", "message": "Аргументи — не JSON", "retryable": True}
    if name not in allowed:
        return {"error": "permission_denied", "message": f"Тобі недоступний {name}", "retryable": False}
    if repeats > MAX_SAME_CALL:
        return {"error": "repeated_call", "message": "Той самий виклик з тими ж аргументами",
                "hint": "Змініть підхід або завершіть відповіддю", "retryable": False}
    return None


async def run_agent(name, prompt, tool_names, message, session, local_tools=None, history=None, guard=None):
    """Запускає агента й повертає його відповідь.

    tool_names   — які інструменти MCP-сервера йому дозволені;
    local_tools  — {назва: {"description", "parameters" (JSON-схема), "run": async (args) -> dict}}: інший агент як інструмент;
    history      — повідомлення розмови: передайте їх знову, і агент продовжить, а не почне з нуля;
    guard        — guard(name, args) -> None або словник-помилка: перевірка в коді перед виконанням.
    """
    local_tools = local_tools or {}
    allowed = set(tool_names) | set(local_tools)
    tools = await available_tools(session, tool_names, local_tools)
    messages = history if history is not None else []
    if not messages:
        messages.append({"role": "system", "content": prompt})
    messages.append({"role": "user", "content": message})
    trace(name, "запит", message)
    last_call, repeats, empty_replies = None, 0, 0

    for step in range(1, MAX_STEPS + 1):
        started = time.time()
        response = await ask_llm(messages, tools)
        reply = response.choices[0].message
        # Токени й час моделі пишемо раз на крок, а не в кожен виклик інструмента — інакше суми завищені
        trace(name, "модель", {"tool_calls": [c.function.name for c in reply.tool_calls or []]}, step=step,
              tokens=response.usage.total_tokens if response.usage else 0,
              latency_ms=round((time.time() - started) * 1000))

        if not reply.tool_calls and not (reply.content or "").strip():
            empty_replies += 1  # модель мовчить: це не відповідь, пробуємо ще
            trace(name, "порожня відповідь", f"спроба {empty_replies}", step=step)
            if empty_replies > MAX_EMPTY_REPLIES:
                usage["stops"] += 1
                raise AgentStopped(f"Агент «{name}» кілька разів поспіль повернув порожню відповідь")
            continue

        if not reply.tool_calls:
            trace(name, "відповідь", reply.content, step=step)
            messages.append({"role": "assistant", "content": reply.content})  # наступна репліка розмови це бачить
            return reply.content

        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in reply.tool_calls]})
        for call in reply.tool_calls:
            tool, raw_args = call.function.name, call.function.arguments
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                args = None
            repeats = repeats + 1 if (tool, raw_args) == last_call else 1
            last_call = (tool, raw_args)

            t0 = time.time()
            output = refusal(tool, args, repeats, allowed)
            if output is None and guard:
                output = guard(tool, args)  # код не пустив виклик: модель дізнається, але обійти не може
            if output is None:
                output = (await local_tools[tool]["run"](args) if tool in local_tools
                          else tool_result(await session.call_tool(tool, args)))  # виклик через MCP
            if tool == "schedule_session" and isinstance(output, dict) and "event_id" in output:
                written.append({**args, "event_id": output["event_id"]})
            failed = isinstance(output, dict) and "error" in output
            usage["tool_calls"] += 1
            usage["tool_errors"] += failed
            tool_log.append((name, tool, failed))
            trace(name, "інструмент", {"name": tool, "args": args, "result": output},
                  step=step, latency_ms=round((time.time() - t0) * 1000))  # лише час самого інструмента
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(output, ensure_ascii=False)})

    trace(name, "стоп", f"агент не впорався за {MAX_STEPS} кроків")
    usage["stops"] += 1
    raise AgentStopped(f"Агент «{name}» не впорався за {MAX_STEPS} кроків")


# ---------- Планувальник як інструмент Оркестратора ----------

def check_blocks(blocks):
    """Перевіряє список сесій {title, start, end, deadline_id}. None — усе гаразд, інакше що саме не так."""
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            return f"blocks[{i}] має бути об'єктом"
        missing = [k for k in ("title", "start", "end", "deadline_id") if not block.get(k)]
        if missing:
            return f"blocks[{i}]: немає полів {missing}"
        try:
            start, end = datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])
        except (ValueError, TypeError):
            return f"blocks[{i}]: час має бути у форматі YYYY-MM-DDTHH:MM"
        if end <= start:
            return f"blocks[{i}]: кінець раніше за початок"
    return None


def check_hours(plan):
    """Години сесій кожного завдання мають дорівнювати його est_hours мінус missing_hours з unscheduled.
    Модель інколи «для запасу» розписує більше, ніж оцінила. None — усе гаразд, інакше що не так."""
    scheduled = {}
    for block in plan["blocks"]:
        hours = (datetime.fromisoformat(block["end"]) - datetime.fromisoformat(block["start"])).total_seconds() / 3600
        scheduled[block["deadline_id"]] = scheduled.get(block["deadline_id"], 0) + hours
    try:
        missing = {}
        for item in plan.get("unscheduled") or []:
            missing[item["deadline_id"]] = missing.get(item["deadline_id"], 0) + float(item.get("missing_hours", 0))
        for task in plan.get("tasks") or []:
            deadline_id, estimate = task["deadline_id"], float(task["est_hours"])
            if deadline_id in scheduled and abs(scheduled[deadline_id] - (estimate - missing.get(deadline_id, 0))) > HOURS_TOLERANCE:
                return (f"для {deadline_id} у blocks {scheduled[deadline_id]:g} год, а est_hours {estimate:g}"
                        f" (у unscheduled не вистачає {missing.get(deadline_id, 0):g}). Години в blocks мають дорівнювати"
                        " est_hours мінус missing_hours: змінь сесії або винеси різницю в unscheduled")
    except (KeyError, TypeError, ValueError, AttributeError):
        return "tasks і unscheduled — списки об'єктів із deadline_id та числами est_hours / missing_hours"
    return None


def parse_plan(text):
    """Відповідь Планувальника → (план, None) або (None, що саме не так).
    Контракт: JSON з blocks (title, start, end, deadline_id), а також tasks і unscheduled."""
    text = (text or "").strip()
    first, last = text.find("{"), text.rfind("}")  # модель може обгорнути JSON у ```json … ``` чи пояснення
    if first == -1 or last <= first:
        return None, "у відповіді немає JSON-об'єкта"
    try:
        plan = json.loads(text[first:last + 1])
    except json.JSONDecodeError as e:
        return None, f"JSON не розібрався: {e}"
    if not isinstance(plan, dict) or not isinstance(plan.get("blocks"), list):
        return None, 'потрібне поле "blocks" — список сесій (може бути порожнім)'
    if problem := check_blocks(plan["blocks"]) or check_hours(plan):
        return None, problem
    plan.setdefault("tasks", [])
    plan.setdefault("unscheduled", [])
    return plan, None


PLAN_STUDY_SPEC = {
    "description": "Попросити Планувальника скласти план: він знає дедлайни (id, час здачі, опис), вільний час "
                   "і оцінює години. Повертає JSON-план tasks/blocks/unscheduled. Викликай спочатку (також щоб "
                   "просто дізнатись, які є дедлайни — дивись tasks), а потім, якщо запис не вдався й потрібен "
                   "інший розподіл.",
    "parameters": {"type": "object", "properties": {"request": {
        "type": "string", "description": "Слова користувача без змін (його мовою). Якщо план змінюють — додай окремо "
                                         "«раніше запропоновано (не вимога): …» з поточним планом"}},
        "required": ["request"]},
}


def planner_tool(session):
    """plan_study: Планувальник як інструмент. Ліміт викликів — на один виклик цієї функції."""
    planner_calls = 0

    async def ask_planner(request, history):
        text = await run_agent("Планувальник", PLANNER_PROMPT, READ_TOOLS, request, session, history=history)
        return parse_plan(text)

    async def plan_study(args):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls > MAX_PLANNER_CALLS:  # обмежуємо делегування в коді, а не в промпті
            return {"error": "planner_budget_exceeded", "message": f"Планувальника вже кликали {MAX_PLANNER_CALLS} рази",
                    "hint": "Запиши, що вмістилось, і поясни користувачу, чого не вистачило", "retryable": False}
        history = []
        try:
            plan, problem = await ask_planner(args.get("request", ""), history)
            if problem:  # одна спроба виправитись у тій самій розмові, без повторного читання Moodle
                trace("Планувальник", "порушено контракт", problem)
                plan, problem = await ask_planner(
                    f"Твоя відповідь не за форматом: {problem}. Поверни лише виправлений JSON.", history)
        except AgentStopped as e:
            return {"error": "planner_failed", "message": str(e),
                    "hint": "Спробуйте сформулювати запит простіше або поясніть користувачу, що план не склався",
                    "retryable": False}
        if problem:
            return {"error": "planner_invalid_plan", "message": f"План не за форматом: {problem}",
                    "hint": "Поясніть користувачу, що план не склався, або запитайте ще раз простіше", "retryable": False}
        return {"plan": plan}

    return {"plan_study": {**PLAN_STUDY_SPEC, "run": plan_study}}


# ---------- схвалення плану людиною ----------

def session_key(block):
    """Сесія як ключ схвалення. Час нормалізуємо так само, як сервер: «18:00» і «18:00:00» — одна сесія."""
    def norm(t):
        return datetime.fromisoformat(t).strftime("%Y-%m-%dT%H:%M")
    return norm(block["start"]), norm(block["end"]), block["deadline_id"]


async def known_deadline_ids(session):
    """id дедлайнів на рік уперед через MCP. None, якщо дізнатись не вдалось: тоді не перевіряємо."""
    result = tool_result(await session.call_tool("get_deadlines", {
        "date_from": today.isoformat(), "date_to": (today + timedelta(days=365)).isoformat()}))
    return {d["id"] for d in result["deadlines"]} if "deadlines" in result else None


SESSION_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
    "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM"}, "deadline_id": {"type": "string"}},
    "required": ["title", "start", "end", "deadline_id"]}

PROPOSE_PLAN_SPEC = {
    "description": "Показати користувачу готовий план і отримати його рішення (консоль сама спитає). "
                   "Записувати в календар можна лише схвалені сесії. Повертає approved true/false і побажання.",
    "parameters": {"type": "object", "properties": {
        "blocks": {"type": "array", "items": SESSION_SCHEMA, "description": "сесії, які пропонуєш записати"},
        "note": {"type": "string", "description": "коротко: припущення й чому саме так"}},
        "required": ["blocks"]},
}


class PlanApproval:
    """Схвалення людиною, яке перевіряє код: у розмові записати можна лише сесії, які користувач бачив у
    propose_plan і схвалив. Слова моделі «користувач погодився» нічого не значать — рахується лише відповідь ask."""

    def __init__(self, ask):
        self.ask = ask  # async (blocks, note) -> (схвалено: bool, побажання: str); у CLI це питання в консолі
        self.approved = set()

    async def propose(self, args, session):
        blocks = args.get("blocks")
        problem = "потрібен непорожній список blocks" if not isinstance(blocks, list) or not blocks else check_blocks(blocks)
        if problem:
            return {"error": "invalid_plan", "message": problem, "hint": "Виправ і виклич propose_plan ще раз",
                    "retryable": True}
        # Опечатка в id не має дійти до користувача: він схвалив би те, що не запишеться
        known = await known_deadline_ids(session)
        unknown = sorted({b["deadline_id"] for b in blocks} - known) if known is not None else []
        if unknown:
            return {"error": "invalid_plan", "message": f"Невідомий deadline_id: {unknown}",
                    "hint": f"Бери id лише з плану Планувальника, символ у символ. Відомі: {sorted(known)}",
                    "retryable": True}
        approved, feedback = await self.ask(blocks, str(args.get("note", "")))
        if not approved:
            return {"approved": False, "feedback": feedback or "користувач відхилив план",
                    "hint": "Врахуй побажання й запропонуй новий план через propose_plan; нічого не записуй"}
        self.approved |= {session_key(b) for b in blocks}
        return {"approved": True, "hint": "Запиши кожну схвалену сесію через schedule_session"}

    def guard(self, name, args):
        if name not in WRITE_TOOLS:
            return None
        try:
            key = session_key(args)
        except (KeyError, ValueError, TypeError):
            return None  # некоректні аргументи сервер відхилить сам
        if key in self.approved:
            self.approved.discard(key)  # схвалення діє на один запис
            return None
        return {"error": "not_approved", "message": "Цю сесію користувач не схвалив",
                "hint": "Покажи її користувачу через propose_plan і дочекайся схвалення", "retryable": True}

    def tool(self, session):
        return {"propose_plan": {**PROPOSE_PLAN_SPEC, "run": lambda args: self.propose(args, session)}}


# ---------- системи, які запускає cli.py та evaluate.py ----------

async def run_multi(request, session):
    """Разовий запуск: Оркестратор сам доводить справу до кінця."""
    try:
        return await run_agent("Оркестратор", ORCHESTRATOR_PROMPT, ORCHESTRATOR_TOOLS, request, session,
                               planner_tool(session))
    except AgentStopped as e:
        return str(e)


async def run_single(request, session):
    try:
        return await run_agent("Один агент", SINGLE_PROMPT, READ_TOOLS + ["schedule_session"], request, session)
    except AgentStopped as e:
        return str(e)


async def chat_turn(session, history, text, approval):
    """Одна репліка розмови: Оркестратор бачить усю history, може перепитати, а записує лише схвалене людиною."""
    try:
        return await run_agent("Оркестратор", ORCHESTRATOR_CHAT_PROMPT, ORCHESTRATOR_TOOLS, text, session,
                               {**planner_tool(session), **approval.tool(session)}, history=history, guard=approval.guard)
    except AgentStopped as e:
        return str(e)


@asynccontextmanager
async def mcp_session():
    async with stdio_client(SERVER) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session
