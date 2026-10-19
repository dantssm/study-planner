# Study planner

Мультиагентна система, яка читає дедлайни з Moodle Calendar (.ics), оцінює час на кожне завдання
і розставляє навчальні блоки в Google Calendar на тиждень — без перетинів, до дедлайнів
і не більше 6 годин навчання на день.

- **Аналітик** (LLM-агент) — читає дедлайни й календар, складає план. Писати не може.
- **Планувальник** (LLM-агент) — перевіряє план за правилами і єдиний записує блоки в календар.
- **MCP-сервер `study_planner`** (`server.py`) — 3 інструменти: `get_deadlines`, `get_events`, `create_study_block`.
- LLM — Gemini API.

Схема: [src/study_planner/docs/architecture.png](src/study_planner/docs/architecture.png)

## Правила планування

| | Правило | Хто перевіряє |
|---|---|---|
| R1 | Блок закінчується до дедлайну | MCP-сервер (помилка `after_deadline`) |
| R2 | Блоки не перетинаються з іншими подіями | MCP-сервер (помилка `time_conflict`) |
| R3 | Не більше 6 год навчання на день | Планувальник |
| R4 | Блоки тільки між 08:00 і 23:00 | Планувальник |
| R5 | Кожне завдання отримує свої години (за типом: квіз 1, лаба 4, есе 3, проєкт 8) | Планувальник |

## Встановлення

```bash
uv sync
cd src/study_planner
cp .env.example .env   # впишіть GEMINI_API_KEY і GOOGLE_CALENDAR_ID
```

Покладіть у `src/study_planner/` файл `credentials.json` (Google Cloud → OAuth client → Desktop app).
`GOOGLE_CALENDAR_ID` — id **окремого тестового** календаря (`…@group.calendar.google.com`):
тести перед кожним запуском очищають його.

Перший вхід у Google і заповнення тестового календаря:

```bash
uv run python google_calendar.py
```

## Запуск (з папки `src/study_planner`)

```bash
# два агенти
uv run python planner.py "Сплануй мені підготовку до дедлайнів цього тижня"

# один агент — для порівняння
uv run python planner.py "Сплануй мені підготовку до дедлайнів цього тижня" --single

# 5 тестових запитів через обидві системи -> results.md
uv run python tests.py
```

Кожен крок агентів пишеться в консоль і в `traces/*.jsonl` (трейсінг).
