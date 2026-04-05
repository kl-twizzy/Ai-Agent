<div align="center">

# AI Agent

Универсальный AI-агент для управления браузером на естественном языке.

Открывает сайты, ищет информацию, взаимодействует с интерфейсами, использует LLM и MCP-style tools для пошагового управления браузером.

</div>

---


https://github.com/user-attachments/assets/2c46b149-28a6-4803-a272-c0fd8b767244




<br />
<br />
<b></b>
<br />
<br />

</div>

---

## Что это такое

Этот проект позволяет управлять браузером обычным текстом.

Пользователь пишет задачу в интерфейсе, например:

- `Открой сайт ЦБ РФ и найди курс доллара`
- `Зайди на YouTube и найди видео про квантовую физику`
- `Открой маркетплейс и найди рабочий ноутбук до 70000 рублей`

После этого агент:

- интерпретирует задачу;
- выбирает следующий инструмент;
- обращается к MCP-layer;
- управляет браузером через Playwright;
- собирает шаги выполнения и возвращает результат.

---

## Возможности

- Управление браузером через естественный язык
- Пошаговый agent loop: `LLM -> MCP -> Browser`
- Работа через видимый браузер в локальном режиме
- Поддержка многошаговых задач
- Подробные шаги выполнения и итоговый отчет
- React frontend для удобного запуска задач
- FastAPI backend для orchestration и API

---

## Архитектура

Проект построен как agentic runtime, а не как набор жестко зашитых сценариев.

Основной поток выполнения:

1. Пользователь отправляет задачу через frontend.
2. Backend запускает browser-agent.
3. LLM выбирает следующий `tool_call`.
4. MCP client отправляет запрос в MCP server.
5. MCP server вызывает browser tool.
6. Агент получает новое состояние страницы и продолжает цикл.

Ключевая связка:

- `frontend` — интерфейс для пользователя
- `main.py` — FastAPI API и orchestration
- `browser/agent.py` — основной agent loop
- `browser/llm.py` — planner и tool-calling логика
- `mcp_server.py` — MCP server
- `browser/mcp_tools.py` — каталог browser tools
- `browser/browser.py` — низкоуровневое управление браузером через Playwright

---

## Стек

- Python
- FastAPI
- Playwright
- MCP-style tools
- Hugging Face Router API
- React
- Vite
- Docker

---

## Структура проекта

```text
project/
├─ browser/
│  ├─ agent.py
│  ├─ browser.py
│  ├─ llm.py
│  └─ mcp_tools.py
├─ frontend/
│  ├─ src/
│  ├─ package.json
│  └─ vite.config.js
├─ main.py
├─ mcp_server.py
├─ models.py
├─ requirements.txt
├─ Dockerfile
└─ docker-compose.yml
```

---

## Локальный запуск

```bash
pip install -r requirements.txt
playwright install chromium
cd frontend
npm install
npm run build
cd ..
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

После запуска открой:

```text
http://localhost:8000
```

---

## Запуск через Docker

```bash
docker compose up --build
```

После этого frontend и backend будут доступны по адресу:

```text
http://localhost:8000
```

---

## Почему проект интересный

Главная идея в том, что это не просто скрипт автоклика, а агентная система.

LLM не управляет браузером напрямую. Она думает в терминах инструментов, а выполнение идет через отдельный MCP-style слой. За счет этого архитектура получается чище, расширяемее и ближе к современным browser-agent системам.

---

## Статус

Сейчас это сильный рабочий прототип универсального браузерного агента с реальным UI, MCP-runtime и видимым управлением браузером.

---

## Автор

Проект подготовлен для хакатона как универсальный AI browser agent.

