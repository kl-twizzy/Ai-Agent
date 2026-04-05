import asyncio
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, AsyncGenerator

from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.stdio import StdioServerParameters, stdio_client

from browser.llm import ask_llm, plan_task
from models import AgentStep, FinalReport, PlanStep, SourceRecord, TaskConstraints

MAX_STEPS = 25
executor = ThreadPoolExecutor(max_workers=1)


class MCPToolClient:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []
        self._initialized = False
        self._init_error: str | None = None
        self._transport = "uninitialized"
        self._local_call_tool_sync = None

    def ensure_started(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            if self._thread and self._thread.is_alive():
                self._ready.wait(timeout=20)
                if not self._initialized:
                    raise RuntimeError(f"Failed to start MCP client runtime: {self._init_error or 'unknown error'}")
                return
            self._thread = threading.Thread(target=self._thread_main, daemon=True, name="mcp-client-loop")
            self._thread.start()
        ready = self._ready.wait(timeout=20)
        if not ready and not self._init_error:
            self._init_error = "MCP client startup timed out"
        if not self._initialized:
            raise RuntimeError(f"Failed to start MCP client runtime: {self._init_error or 'unknown error'}")

    def _thread_main(self):
        try:
            self._initialize_transport()
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        finally:
            self._ready.set()
        if self._initialized and self._transport == "stdio" and self._loop is not None:
            self._loop.run_forever()

    def _initialize_transport(self):
        preferred_transport = (os.getenv("MCP_TRANSPORT") or "").strip().lower()
        if not preferred_transport:
            preferred_transport = "local" if sys.platform == "win32" else "stdio"

        if preferred_transport == "local":
            self._initialize_local_transport()
            self._transport = "local"
            self._initialized = True
            self._init_error = "Using local MCP transport on Windows by default"
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._initialize_stdio_transport())
            self._transport = "stdio"
            self._initialized = True
            return
        except PermissionError as exc:
            self._init_error = f"stdio transport unavailable on this Windows setup: {exc}"
        except Exception as exc:
            self._init_error = f"stdio transport failed: {type(exc).__name__}: {exc}"
        finally:
            if self._transport != "stdio" and self._loop is not None:
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._loop = None

        self._initialize_local_transport()
        self._transport = "local"
        self._initialized = True

    async def _initialize_stdio_transport(self):
        project_root = Path(__file__).resolve().parent.parent
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-Xutf8", str(project_root / "mcp_server.py")],
            encoding="utf-8",
            encoding_error_handler="replace",
        )
        self._exit_stack = AsyncExitStack()
        read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self._session = ClientSession(read_stream, write_stream)
        await self._session.initialize()
        tools_result = await self._session.list_tools()
        self._tools = [tool.model_dump() if hasattr(tool, "model_dump") else tool for tool in tools_result.tools]

    def _initialize_local_transport(self):
        from mcp_server import call_mcp_tool_sync, list_mcp_tools

        self._local_call_tool_sync = call_mcp_tool_sync
        self._tools = [
            tool.model_dump() if hasattr(tool, "model_dump") else tool
            for tool in list_mcp_tools()
        ]

    def _run_coroutine(self, coroutine):
        self.ensure_started()
        if self._loop is None:
            raise RuntimeError("Async MCP loop is not available for stdio transport")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=120)

    def list_tools(self) -> list[dict[str, Any]]:
        self.ensure_started()
        return list(self._tools)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ensure_started()
        if self._transport == "local":
            if self._local_call_tool_sync is None:
                raise RuntimeError("Local MCP transport is not initialized")
            result = self._local_call_tool_sync(name, arguments or {})
            return self._parse_call_tool_result(result)
        return self._run_coroutine(self._call_tool(name, arguments or {}))

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._transport != "stdio" or self._session is None:
            raise RuntimeError(f"Async MCP calls are only supported for stdio transport, got {self._transport}")
        result = await self._session.call_tool(name, arguments)
        return self._parse_call_tool_result(result)

    def _parse_call_tool_result(self, result: mcp_types.CallToolResult) -> dict[str, Any]:
        if result.structuredContent is not None:
            payload = dict(result.structuredContent)
        else:
            payload = {}
            for item in result.content:
                if getattr(item, "type", None) == "text":
                    try:
                        payload = json.loads(item.text)
                    except Exception:
                        payload = {"status": "ok", "message": item.text}
                elif getattr(item, "type", None) == "image":
                    payload = {"status": "ok", "image": item.data, "mime_type": item.mimeType}
        if result.isError:
            payload.setdefault("status", "error")
        return payload


_mcp_client = MCPToolClient()
_mcp_run_counter = 0
_MARKETPLACE_URLS = {
    "яндекс маркет": "https://market.yandex.ru/",
    "yandex market": "https://market.yandex.ru/",
    "market.yandex": "https://market.yandex.ru/",
    "озон": "https://www.ozon.ru/",
    "ozon": "https://www.ozon.ru/",
    "wildberries": "https://www.wildberries.ru/",
    "вайлдберриз": "https://www.wildberries.ru/",
    "wb": "https://www.wildberries.ru/",
    "мегамаркет": "https://megamarket.ru/",
    "сбермегамаркет": "https://megamarket.ru/",
    "avito": "https://www.avito.ru/",
    "авито": "https://www.avito.ru/",
}
_CLOTHING_COLLECTION_DEFAULTS = ["куртка", "лонгслив", "джинсы", "кроссовки", "сумка"]
_PRODUCT_KEYWORDS = [
    "ноутбук", "смартфон", "телефон", "платье", "куртка", "кроссовки", "джинсы",
    "футболка", "лонгслив", "рубашка", "брюки", "ботинки", "рюкзак", "сумка",
    "часы", "наушники", "кофта", "худи", "пальто", "юбка",
]


# Clean runtime aliases override the legacy mojibake strings above.
_MARKETPLACE_URLS = {
    "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442": "https://market.yandex.ru/",
    "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442\u0435": "https://market.yandex.ru/",
    "yandex market": "https://market.yandex.ru/",
    "market.yandex": "https://market.yandex.ru/",
    "\u043e\u0437\u043e\u043d": "https://www.ozon.ru/",
    "ozon": "https://www.ozon.ru/",
    "\u0432\u0430\u0439\u043b\u0434\u0431\u0435\u0440\u0440\u0438\u0437": "https://www.wildberries.ru/",
    "\u0432\u0430\u0439\u043b\u0434\u0431\u0435\u0440\u0438\u0437": "https://www.wildberries.ru/",
    "wildberries": "https://www.wildberries.ru/",
    "wb": "https://www.wildberries.ru/",
    "\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442": "https://megamarket.ru/",
    "\u0441\u0431\u0435\u0440\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442": "https://megamarket.ru/",
    "\u0430\u0432\u0438\u0442\u043e": "https://www.avito.ru/",
    "avito": "https://www.avito.ru/",
}
_CLOTHING_COLLECTION_DEFAULTS = [
    "\u043a\u0443\u0440\u0442\u043a\u0430",
    "\u043b\u043e\u043d\u0433\u0441\u043b\u0438\u0432",
    "\u0434\u0436\u0438\u043d\u0441\u044b",
    "\u043a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438",
    "\u0441\u0443\u043c\u043a\u0430",
]
_PRODUCT_KEYWORDS = [
    "\u043d\u043e\u0443\u0442\u0431\u0443\u043a",
    "\u0441\u043c\u0430\u0440\u0442\u0444\u043e\u043d",
    "\u0442\u0435\u043b\u0435\u0444\u043e\u043d",
    "\u043f\u043b\u0430\u0442\u044c\u0435",
    "\u043a\u0443\u0440\u0442\u043a\u0430",
    "\u043a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438",
    "\u0434\u0436\u0438\u043d\u0441\u044b",
    "\u0444\u0443\u0442\u0431\u043e\u043b\u043a\u0430",
    "\u043b\u043e\u043d\u0433\u0441\u043b\u0438\u0432",
    "\u0440\u0443\u0431\u0430\u0448\u043a\u0430",
    "\u0431\u0440\u044e\u043a\u0438",
    "\u0431\u043e\u0442\u0438\u043d\u043a\u0438",
    "\u0440\u044e\u043a\u0437\u0430\u043a",
    "\u0441\u0443\u043c\u043a\u0430",
    "\u0447\u0430\u0441\u044b",
    "\u043d\u0430\u0443\u0448\u043d\u0438\u043a\u0438",
    "\u043a\u043e\u0444\u0442\u0430",
    "\u0445\u0443\u0434\u0438",
    "\u043f\u0430\u043b\u044c\u0442\u043e",
    "\u044e\u0431\u043a\u0430",
]
_RUSSIAN_MARKETPLACE_DEFAULT = "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442"


def _normalize_marketplace_name(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.strip().lower()
    aliases = {
        "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442": "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442",
        "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442\u0435": "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442",
        "yandex market": "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442",
        "market.yandex": "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442",
        "\u043e\u0437\u043e\u043d": "\u043e\u0437\u043e\u043d",
        "ozon": "\u043e\u0437\u043e\u043d",
        "\u0432\u0430\u0439\u043b\u0434\u0431\u0435\u0440\u0440\u0438\u0437": "wildberries",
        "\u0432\u0430\u0439\u043b\u0434\u0431\u0435\u0440\u0438\u0437": "wildberries",
        "wildberries": "wildberries",
        "wb": "wildberries",
        "\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442": "\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442",
        "\u0441\u0431\u0435\u0440\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442": "\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442",
        "\u0430\u0432\u0438\u0442\u043e": "\u0430\u0432\u0438\u0442\u043e",
        "avito": "\u0430\u0432\u0438\u0442\u043e",
    }
    return aliases.get(lowered, lowered)


def _should_leave_browser_open() -> bool:
    keep_open = (os.getenv("KEEP_BROWSER_OPEN") or "true").strip().lower() in {"1", "true", "yes", "on"}
    headless = (os.getenv("BROWSER_HEADLESS") or "false").strip().lower() in {"1", "true", "yes", "on"}
    return keep_open and not headless


def _new_plan() -> list[PlanStep]:
    return [
        PlanStep(id="observe", title="Observe the current browser page", status="pending"),
        PlanStep(id="reason", title="Let the LLM choose the next MCP tool", status="pending"),
        PlanStep(id="act", title="Execute the selected MCP tool", status="pending"),
        PlanStep(id="verify", title="Verify the browser changed after the tool call", status="pending"),
        PlanStep(id="report", title="Build the final report", status="pending"),
    ]


def _set_plan(plan: list[PlanStep], step_id: str, status: str, details: str | None = None):
    for item in plan:
        if item.id == step_id:
            item.status = status
            item.details = details
            break


def _log(audit_log: list[str], message: str):
    audit_log.append(message)


def _record_history(history: list[dict[str, Any]], step_num: int, action: dict[str, Any], outcome: str):
    history.append(
        {
            "step": step_num,
            "action": action.get("action", "unknown"),
            "tool_name": action.get("tool_name"),
            "arguments": dict(action.get("arguments", {})),
            "description": action.get("description", ""),
            "llm_model": action.get("llm_model"),
            "llm_mode": action.get("llm_mode"),
            "outcome": outcome,
        }
    )


def _recent_repeated_tool(
    history: list[dict[str, Any]],
    tool_name: str,
    query: str | None = None,
    threshold: int = 2,
) -> bool:
    if len(history) < threshold:
        return False
    recent = history[-threshold:]
    matched = 0
    for item in recent:
        if item.get("tool_name") != tool_name:
            return False
        if query is not None:
            args = item.get("arguments", {}) or {}
            item_query = str(args.get("query") or args.get("text") or "").lower()
            if query.lower() not in item_query:
                return False
        matched += 1
    return matched == threshold


def _emit_step(step_callback, step: AgentStep):
    if not step_callback:
        return
    step_callback(
        {
            "type": "step",
            "step": step.step,
            "action": step.action,
            "description": step.description,
            "tool_name": step.tool_name,
            "tool_arguments": step.tool_arguments,
            "llm_model": step.llm_model,
            "llm_mode": step.llm_mode,
            "verification": step.verification,
            "outcome": step.outcome,
        }
    )


def _prepare_runtime():
    global _mcp_run_counter
    _mcp_client.ensure_started()
    if _mcp_run_counter > 0 and _should_leave_browser_open():
        _mcp_client.call_tool("new_tab", {})
    _mcp_run_counter += 1


def _tool_result_message(tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str:
    if result.get("status") == "error":
        raise ValueError(result.get("message", f"Tool {tool_name} failed"))
    if tool_name == "find_elements":
        matches = result.get("matches", [])
        if not matches:
            return f"No visible interactive elements matched query '{arguments.get('query', '')}'"
        preview = ", ".join(
            (match.get("text") or match.get("placeholder") or match.get("ariaLabel") or match.get("selector") or "element")[:80]
            for match in matches[:3]
        )
        return f"Tool {tool_name} found {len(matches)} matching elements: {preview}"
    return result.get("message") or f"Tool {tool_name} completed successfully"


def _execute_action(action: dict[str, Any]) -> str:
    action_type = action.get("action")
    if action_type == "wait":
        seconds = float(action.get("seconds", 1.5))
        time.sleep(seconds)
        return f"Waited {seconds} seconds"
    if action_type == "done":
        return action.get("result", "Task completed")
    if action_type == "error":
        return action.get("description", "Agent reported an error")

    tool_name = action.get("tool_name")
    arguments = action.get("arguments", {})
    result = _mcp_client.call_tool(tool_name, arguments)
    return _tool_result_message(tool_name, arguments, result)


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(action)
    normalized["tool_name"] = normalized.get("tool_name") or normalized.get("action")
    arguments = normalized.get("arguments") if isinstance(normalized.get("arguments"), dict) else {}
    if normalized["tool_name"] == "goto":
        arguments.setdefault("url", normalized.get("url"))
    if normalized["tool_name"] in {"find_elements", "click_text"}:
        arguments.setdefault("query", normalized.get("query"))
    if normalized["tool_name"] == "click_first_search_result":
        arguments = {}
    if normalized["tool_name"] == "type_into_text":
        arguments.setdefault("query", normalized.get("query"))
        arguments.setdefault("text", normalized.get("text", ""))
        arguments.setdefault("submit", bool(normalized.get("submit", False)))
    if normalized["tool_name"] == "press_key":
        arguments.setdefault("key", normalized.get("key"))
    if normalized["tool_name"] == "scroll":
        arguments.setdefault("direction", normalized.get("direction", "down"))
        arguments.setdefault("amount", int(normalized.get("amount", 300)))
    if normalized["tool_name"] == "wait_for_text":
        arguments.setdefault("text", normalized.get("text"))
        arguments.setdefault("timeout_ms", int(normalized.get("timeout_ms", 10000)))
    if normalized["tool_name"] == "navigate_history":
        arguments.setdefault("direction", normalized.get("direction", "back"))
    normalized["arguments"] = {key: value for key, value in arguments.items() if value is not None}
    return normalized


def _minimal_bootstrap_action(task: str, page_state: dict[str, Any], constraints: TaskConstraints | None = None) -> dict[str, Any] | None:
    if page_state.get("url") not in {"", "about:blank"}:
        return None
    if constraints and constraints.requested_url:
        return {
            "action": "goto",
            "tool_name": "goto",
            "arguments": {"url": constraints.requested_url},
            "description": f"Open the target site directly for the task: {task[:80]}",
            "llm_model": None,
            "llm_mode": "bootstrap",
        }
    return {
        "action": "goto",
        "tool_name": "goto",
        "arguments": {"url": "https://ya.ru/" if (os.getenv("SEARCH_ENGINE") or "").lower() == "yandex" else "https://www.google.com/"},
        "description": f"Open a neutral starting page for the task: {task[:80]}",
        "llm_model": None,
        "llm_mode": "bootstrap",
    }


def _extract_price_limit(task: str) -> float | None:
    match = re.search(r"(?:до|не дороже|максимум)\s*(\d[\d\s]*)", task.lower())
    if not match:
        return None
    try:
        return float(match.group(1).replace(" ", ""))
    except Exception:
        return None


def _extract_marketplace(task: str) -> tuple[str | None, str | None]:
    lowered = task.lower()
    for name, url in _MARKETPLACE_URLS.items():
        if name in lowered:
            return name, url
    if "marketplace" in lowered or "маркетплейс" in lowered:
        return "яндекс маркет", "https://market.yandex.ru/"
    return None, None


def _extract_explicit_url(task: str) -> str | None:
    match = re.search(r"\b(?:https?://)?([a-z0-9-]+\.[a-z]{2,})(?:/|$)", task.lower())
    if not match:
        return None
    domain = match.group(1)
    return f"https://{domain}"


def _extract_shopping_items(task: str) -> list[str]:
    lowered = task.lower()
    items: list[str] = []
    for keyword in _PRODUCT_KEYWORDS:
        if keyword in lowered and keyword not in items:
            items.append(keyword)

    if any(word in lowered for word in ("лук", "образ", "outfit")) and not items:
        items.extend(_CLOTHING_COLLECTION_DEFAULTS)

    tail = lowered
    for prefix in (
        "добавь в корзину",
        "добавь",
        "собери мне",
        "собери",
        "найди мне",
        "найди",
    ):
        if prefix in tail:
            tail = tail.split(prefix, 1)[1].strip()
            break
    for marketplace in _MARKETPLACE_URLS:
        if marketplace in tail:
            tail = tail.replace(marketplace, " ")
    tail = re.sub(r"\bяндекс\s+маркет\w*\b", " ", tail)
    tail = re.sub(r"\bозон\w*\b", " ", tail)
    tail = re.sub(r"\bwildberries\w*\b", " ", tail)
    tail = re.sub(r"\bвайлдберриз\w*\b", " ", tail)
    tail = re.sub(r"\bна\b", " ", tail)
    tail = re.sub(r"\bв\b", " ", tail)
    tail = re.sub(r"\bкорзин[ауеы]\b", " ", tail)
    tail = re.sub(r"\bдо\s*\d[\d\s]*\b", " ", tail)
    tail = re.sub(r"^\s*е\b", " ", tail)
    tail = re.sub(r"\s+", " ", tail).strip(" :-,")
    if "," in tail or " и " in tail:
        for part in re.split(r",| и | \+ ", tail):
            clean = re.sub(r"[^a-zA-Zа-яА-Я0-9ёЁ -]", " ", part).strip()
            if len(clean) >= 3 and clean not in items:
                items.append(clean)
    return items[:8]


def _extract_product_query(task: str, shopping_items: list[str], target_marketplace: str | None) -> str:
    lowered = task.lower()
    cleaned = lowered
    for prefix in (
        "открой", "зайди", "перейди", "найди", "подбери", "собери", "добавь",
        "мне", "на", "в", "маркетплейс", "корзину", "корзина",
    ):
        cleaned = re.sub(rf"\b{re.escape(prefix)}\b", " ", cleaned)
    if target_marketplace:
        cleaned = cleaned.replace(target_marketplace, " ")
    cleaned = re.sub(r"\bдо\s*\d[\d\s]*\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-,")
    if shopping_items:
        if len(shopping_items) == 1:
            return shopping_items[0]
        return ", ".join(shopping_items)
    return cleaned or task


def _build_task_breakdown(constraints: TaskConstraints) -> list[str]:
    if constraints.wants_add_to_cart and constraints.shopping_items:
        return [
            "Open the target marketplace or locate it in search",
            "Search for the next missing shopping item",
            "Open a relevant product card",
            "Add the item to cart when the add-to-cart control is visible",
            "Repeat until every requested item is processed",
        ]
    if constraints.wants_collection and constraints.shopping_items:
        return [
            "Open the target marketplace or locate it in search",
            "Search for the next clothing category in the outfit",
            "Open a relevant product result for that category",
            "Repeat for all clothing categories in the requested look",
            "Finish after links for all categories were opened or collected",
        ]
    if constraints.product_query:
        return [
            "Open the target marketplace or locate it in search",
            "Search for the requested product",
            "Open a relevant result card",
            "Refine or continue browsing if the page is not relevant yet",
        ]
    return [
        "Observe the page",
        "Choose one MCP tool",
        "Execute it",
        "Verify the page changed",
    ]


def _runtime_constraints(task: str) -> TaskConstraints:
    lowered = task.lower()
    target_marketplace, requested_url = _extract_marketplace(task)
    explicit_url = _extract_explicit_url(task)
    if explicit_url:
        requested_url = explicit_url
    shopping_items = _extract_shopping_items(task)
    wants_add_to_cart = any(phrase in lowered for phrase in ("в корзину", "добавь", "добавить в корзину"))
    wants_collection = any(phrase in lowered for phrase in ("лук", "образ", "outfit", "подборку одежды"))
    product_query = _extract_product_query(task, shopping_items, target_marketplace)
    is_shopping = bool(
        target_marketplace
        or shopping_items
        or wants_add_to_cart
        or wants_collection
        or any(keyword in lowered for keyword in _PRODUCT_KEYWORDS)
        or "маркетплейс" in lowered
    )
    query_type = "shopping" if is_shopping else "general"
    intent = "shopping_collection" if wants_collection else "shopping" if is_shopping else "generic"
    constraints = TaskConstraints(
        raw_query=task,
        search_query=(f"{target_marketplace} {product_query}".strip() if target_marketplace and product_query else product_query or task),
        intent=intent,
        query_type=query_type,
        product_query=product_query if is_shopping else None,
        marketplaces=[target_marketplace] if target_marketplace else [],
        shopping_items=shopping_items,
        target_marketplace=target_marketplace,
        wants_add_to_cart=wants_add_to_cart,
        wants_collection=wants_collection,
        max_price=_extract_price_limit(task),
        requested_url=requested_url,
        sensitive_action=wants_add_to_cart,
        is_long_task=len(task.split()) >= 8,
        task_breakdown=[],
    )
    constraints.task_breakdown = _build_task_breakdown(constraints)
    return constraints


def _merge_constraints(primary: TaskConstraints | None, fallback: TaskConstraints) -> TaskConstraints:
    if primary is None:
        return fallback

    data = fallback.model_dump()
    planned = primary.model_dump()
    for key, value in planned.items():
        if value in (None, "", [], {}):
            continue
        data[key] = value
    merged = TaskConstraints(**data)
    if not merged.task_breakdown:
        merged.task_breakdown = _build_task_breakdown(merged)
    return merged


def _is_search_home(page_state: dict[str, Any]) -> bool:
    url = (page_state.get("url") or "").lower()
    if any(host in url for host in ("market.yandex.ru", "ozon.ru", "wildberries.ru", "megamarket.ru", "avito.ru")):
        return False
    return any(host in url for host in ("ya.ru", "yandex.ru", "google.", "bing.com"))


def _is_target_marketplace_page(page_state: dict[str, Any], constraints: TaskConstraints) -> bool:
    url = (page_state.get("url") or "").lower()
    if not constraints.target_marketplace:
        return False
    marketplace = constraints.target_marketplace.lower()
    if marketplace in {"яндекс маркет", "yandex market", "market.yandex"}:
        return "market.yandex.ru" in url
    if marketplace in {"озон", "ozon"}:
        return "ozon.ru" in url
    if marketplace in {"wildberries", "вайлдберриз", "wb"}:
        return "wildberries.ru" in url
    if marketplace in {"мегамаркет", "сбермегамаркет"}:
        return "megamarket.ru" in url
    if marketplace in {"авито", "avito"}:
        return "avito.ru" in url
    return False


def _build_primary_search_query(constraints: TaskConstraints, task: str) -> str:
    if constraints.target_marketplace and constraints.product_query:
        return f"{constraints.target_marketplace} {constraints.product_query}".strip()
    if constraints.product_query:
        return constraints.product_query
    return constraints.search_query or task


def _extract_price_limit(task: str) -> float | None:
    lowered = task.lower()
    patterns = [
        r"(?:\b\u0434\u043e\b|\b\u043d\u0435 \u0434\u043e\u0440\u043e\u0436\u0435\b|\b\u043c\u0430\u043a\u0441\u0438\u043c\u0443\u043c\b)\s*(\d[\d\s]*)",
        r"(\d[\d\s]*)\s*(?:\u0440\u0443\u0431|\u0440|rub)\b",
        r"(\d[\d\s]*)\s*k\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        raw = match.group(1).replace(" ", "")
        try:
            value = float(raw)
            if pattern.endswith(r"k\b"):
                value *= 1000
            return value
        except Exception:
            continue
    return None


def _extract_marketplace(task: str) -> tuple[str | None, str | None]:
    lowered = task.lower()
    for name, url in _MARKETPLACE_URLS.items():
        if name in lowered:
            return _normalize_marketplace_name(name), url
    if "marketplace" in lowered or "\u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441" in lowered:
        return _RUSSIAN_MARKETPLACE_DEFAULT, _MARKETPLACE_URLS[_RUSSIAN_MARKETPLACE_DEFAULT]
    return None, None


def _extract_shopping_items(task: str) -> list[str]:
    lowered = task.lower()
    items: list[str] = []
    for keyword in _PRODUCT_KEYWORDS:
        if keyword in lowered and keyword not in items:
            items.append(keyword)

    if any(word in lowered for word in ("\u043b\u0443\u043a", "\u043e\u0431\u0440\u0430\u0437", "outfit")) and not items:
        items.extend(_CLOTHING_COLLECTION_DEFAULTS)

    tail = lowered
    for prefix in (
        "\u0434\u043e\u0431\u0430\u0432\u044c \u0432 \u043a\u043e\u0440\u0437\u0438\u043d\u0443",
        "\u0434\u043e\u0431\u0430\u0432\u044c",
        "\u0441\u043e\u0431\u0435\u0440\u0438 \u043c\u043d\u0435",
        "\u0441\u043e\u0431\u0435\u0440\u0438",
        "\u043d\u0430\u0439\u0434\u0438 \u043c\u043d\u0435",
        "\u043d\u0430\u0439\u0434\u0438",
        "\u043e\u0442\u043a\u0440\u043e\u0439",
    ):
        if prefix in tail:
            tail = tail.split(prefix, 1)[1].strip()
            break

    for marketplace in _MARKETPLACE_URLS:
        tail = tail.replace(marketplace, " ")

    tail = re.sub(r"\b(?:\u043d\u0430|\u0432|\u0438|\u043c\u043d\u0435)\b", " ", tail)
    tail = re.sub(r"\b\u043a\u043e\u0440\u0437\u0438\u043d[а-я]*\b", " ", tail)
    tail = re.sub(r"\b\u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441[а-я]*\b", " ", tail)
    tail = re.sub(r"\b(?:\u0434\u043e|price|max)\s*\d[\d\s]*\b", " ", tail)
    tail = re.sub(r"\s+", " ", tail).strip(" :-,")

    if "," in tail or " \u0438 " in tail:
        for part in re.split(r",| \u0438 | \+ ", tail):
            clean = re.sub(r"[^a-zA-Zа-яА-Я0-9ёЁ -]", " ", part).strip()
            if len(clean) >= 3 and clean not in items:
                items.append(clean)

    return items[:8]


def _extract_product_query(task: str, shopping_items: list[str], target_marketplace: str | None) -> str:
    lowered = task.lower()
    cleaned = lowered
    for prefix in (
        "\u043e\u0442\u043a\u0440\u043e\u0439",
        "\u0437\u0430\u0439\u0434\u0438",
        "\u043f\u0435\u0440\u0435\u0439\u0434\u0438",
        "\u043d\u0430\u0439\u0434\u0438",
        "\u043f\u043e\u0434\u0431\u0435\u0440\u0438",
        "\u0441\u043e\u0431\u0435\u0440\u0438",
        "\u0434\u043e\u0431\u0430\u0432\u044c",
        "\u043c\u043d\u0435",
        "\u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441",
        "\u043a\u043e\u0440\u0437\u0438\u043d\u0443",
        "\u043a\u043e\u0440\u0437\u0438\u043d\u0430",
    ):
        cleaned = re.sub(rf"\b{re.escape(prefix)}\b", " ", cleaned)

    if target_marketplace:
        cleaned = cleaned.replace(target_marketplace.lower(), " ")
    for marketplace in _MARKETPLACE_URLS:
        cleaned = cleaned.replace(marketplace, " ")

    cleaned = re.sub(r"\b(?:\u043d\u0430|\u0432)\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:\u0434\u043e|price|max)\s*\d[\d\s]*\b", " ", cleaned)
    cleaned = re.sub(r"\b\u0440\u0443\u0431(?:\u043b(?:\u0435\u0439|\u044f|\u0435\u0432)?)?\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-,")

    if shopping_items:
        if len(shopping_items) == 1:
            return shopping_items[0]
        return ", ".join(shopping_items)
    return cleaned or task


def _runtime_constraints(task: str) -> TaskConstraints:
    lowered = task.lower()
    target_marketplace, requested_url = _extract_marketplace(task)
    explicit_url = _extract_explicit_url(task)
    if explicit_url:
        requested_url = explicit_url

    shopping_items = _extract_shopping_items(task)
    wants_add_to_cart = any(
        phrase in lowered
        for phrase in (
            "\u0432 \u043a\u043e\u0440\u0437\u0438\u043d\u0443",
            "\u0434\u043e\u0431\u0430\u0432\u044c",
            "\u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0432 \u043a\u043e\u0440\u0437\u0438\u043d\u0443",
        )
    )
    wants_collection = any(
        phrase in lowered
        for phrase in (
            "\u043b\u0443\u043a",
            "\u043e\u0431\u0440\u0430\u0437",
            "outfit",
            "\u043f\u043e\u0434\u0431\u043e\u0440\u043a\u0443 \u043e\u0434\u0435\u0436\u0434\u044b",
        )
    )
    product_query = _extract_product_query(task, shopping_items, target_marketplace)
    is_shopping = bool(
        target_marketplace
        or shopping_items
        or wants_add_to_cart
        or wants_collection
        or any(keyword in lowered for keyword in _PRODUCT_KEYWORDS)
        or "marketplace" in lowered
        or "\u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441" in lowered
    )
    query_type = "shopping" if is_shopping else "general"
    intent = "shopping_collection" if wants_collection else "shopping" if is_shopping else "generic"

    if requested_url and product_query:
        search_query = product_query
    elif target_marketplace and product_query:
        search_query = f"{target_marketplace} {product_query}".strip()
    else:
        search_query = product_query or task

    constraints = TaskConstraints(
        raw_query=task,
        search_query=search_query,
        intent=intent,
        query_type=query_type,
        product_query=product_query if is_shopping else None,
        marketplaces=[target_marketplace] if target_marketplace else [],
        shopping_items=shopping_items,
        target_marketplace=target_marketplace,
        wants_add_to_cart=wants_add_to_cart,
        wants_collection=wants_collection,
        max_price=_extract_price_limit(task),
        requested_url=requested_url,
        sensitive_action=wants_add_to_cart,
        is_long_task=len(task.split()) >= 8,
        task_breakdown=[],
    )
    constraints.task_breakdown = _build_task_breakdown(constraints)
    return constraints


def _merge_constraints(primary: TaskConstraints | None, fallback: TaskConstraints) -> TaskConstraints:
    if primary is None:
        return fallback

    data = fallback.model_dump()
    planned = primary.model_dump()
    raw_task = (fallback.raw_query or "").lower()
    generic_marketplace_request = (
        ("marketplace" in raw_task or "\u043c\u0430\u0440\u043a\u0435\u0442\u043f\u043b\u0435\u0439\u0441" in raw_task)
        and not any(name in raw_task for name in _MARKETPLACE_URLS)
    )

    for key, value in planned.items():
        if value in (None, "", [], {}):
            continue
        if generic_marketplace_request and key in {"target_marketplace", "requested_url", "marketplaces"}:
            continue
        data[key] = value

    merged = TaskConstraints(**data)
    if not merged.task_breakdown:
        merged.task_breakdown = _build_task_breakdown(merged)
    return merged


def _is_target_marketplace_page(page_state: dict[str, Any], constraints: TaskConstraints) -> bool:
    url = (page_state.get("url") or "").lower()
    marketplace = _normalize_marketplace_name(constraints.target_marketplace)
    if not marketplace:
        return False
    if marketplace == "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0430\u0440\u043a\u0435\u0442":
        return "market.yandex.ru" in url
    if marketplace == "\u043e\u0437\u043e\u043d":
        return "ozon.ru" in url
    if marketplace == "wildberries":
        return "wildberries.ru" in url
    if marketplace == "\u043c\u0435\u0433\u0430\u043c\u0430\u0440\u043a\u0435\u0442":
        return "megamarket.ru" in url
    if marketplace == "\u0430\u0432\u0438\u0442\u043e":
        return "avito.ru" in url
    return False


def _build_primary_search_query(constraints: TaskConstraints, task: str) -> str:
    if constraints.requested_url and constraints.product_query:
        return constraints.product_query
    if constraints.target_marketplace and constraints.product_query:
        return f"{constraints.target_marketplace} {constraints.product_query}".strip()
    if constraints.product_query:
        return constraints.product_query
    return constraints.search_query or task


def _shopping_progress(constraints: TaskConstraints, history: list[dict[str, Any]]) -> dict[str, Any]:
    items = constraints.shopping_items or ([constraints.product_query] if constraints.product_query else [])
    completed: list[str] = []
    for item in items:
        normalized = item.lower()
        for step in history:
            args = step.get("arguments", {}) or {}
            outcome = (step.get("outcome") or "").lower()
            if args.get("text") and normalized in str(args.get("text")).lower():
                if normalized not in completed:
                    completed.append(normalized)
            elif args.get("query") and normalized in str(args.get("query")).lower() and (
                "clicked" in outcome or "opened" in outcome or "typed" in outcome
            ):
                if normalized not in completed:
                    completed.append(normalized)
    remaining = [item for item in items if item.lower() not in completed]
    return {"completed": completed, "remaining": remaining, "current_target": remaining[0] if remaining else None}


def _local_state_delta(before_state: dict[str, Any], after_state: dict[str, Any]) -> tuple[bool, str]:
    before_url = before_state.get("url", "") or ""
    after_url = after_state.get("url", "") or ""
    before_title = (before_state.get("title") or "").strip()
    after_title = (after_state.get("title") or "").strip()
    before_text = before_state.get("text", "") or ""
    after_text = after_state.get("text", "") or ""
    before_elements = before_state.get("elements", []) or []
    after_elements = after_state.get("elements", []) or []

    notes: list[str] = []
    meaningful = False
    if before_url != after_url:
        meaningful = True
        notes.append(f"url changed to {after_url}")
    if before_title != after_title and after_title:
        meaningful = True
        notes.append(f"title changed to '{after_title[:80]}'")
    if before_text != after_text:
        delta = abs(len(after_text) - len(before_text))
        if delta > 80:
            meaningful = True
            notes.append("page text changed noticeably")
    if len(before_elements) != len(after_elements):
        meaningful = True
        notes.append("interactive elements changed")
    if not notes:
        notes.append("page state did not change much")
    return meaningful, "; ".join(notes)


def _heuristic_fallback_action(task: str, constraints: TaskConstraints, page_state: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    url = (page_state.get("url") or "").lower()
    text = (page_state.get("text") or "").lower()
    elements = page_state.get("elements", []) or []
    previous_actions = [item.get("action") for item in history]
    last_action = previous_actions[-1] if previous_actions else None
    search_has_query = any(token in url for token in ("text=", "q=", "query=", "search?"))
    shopping = _shopping_progress(constraints, history)
    current_target = shopping["current_target"] or constraints.product_query or task
    primary_search_query = _build_primary_search_query(constraints, task)

    if url in {"", "about:blank"}:
        if constraints.requested_url:
            return {
                "action": "goto",
                "tool_name": "goto",
                "arguments": {"url": constraints.requested_url},
                "description": "Open the target marketplace directly [heuristic fallback]",
                "llm_mode": "heuristic",
                "llm_model": None,
            }
        return _minimal_bootstrap_action(task, page_state, constraints) or {
            "action": "goto",
            "tool_name": "goto",
            "arguments": {"url": "https://ya.ru/"},
            "description": "Open a neutral search page [heuristic fallback]",
            "llm_mode": "heuristic",
            "llm_model": None,
        }

    if _is_search_home(page_state) and not search_has_query and "type_into_text" not in previous_actions:
        return {
            "action": "type_into_text",
            "tool_name": "type_into_text",
            "arguments": {"query": "поиск", "text": primary_search_query, "submit": True},
            "description": "Type the user request into the search field [heuristic fallback]",
            "llm_mode": "heuristic",
            "llm_model": None,
        }

    if _is_search_home(page_state) and not search_has_query and last_action == "type_into_text":
        return {
            "action": "press_key",
            "tool_name": "press_key",
            "arguments": {"key": "Enter"},
            "description": "Submit the search query with Enter [heuristic fallback]",
            "llm_mode": "heuristic",
            "llm_model": None,
        }

    if _is_search_home(page_state) and search_has_query and "click_first_search_result" not in previous_actions:
        if constraints.target_marketplace:
            return {
                "action": "click_text",
                "tool_name": "click_text",
                "arguments": {"query": constraints.target_marketplace},
                "description": "Open the marketplace result from search [heuristic fallback]",
                "llm_mode": "heuristic",
                "llm_model": None,
            }
        return {
            "action": "click_first_search_result",
            "tool_name": "click_first_search_result",
            "arguments": {},
            "description": "Open the first visible external search result [heuristic fallback]",
            "llm_mode": "heuristic",
            "llm_model": None,
        }

    if _is_search_home(page_state) and search_has_query and "click_text" not in previous_actions:
        return {
            "action": "click_text",
            "tool_name": "click_text",
            "arguments": {"query": constraints.target_marketplace or current_target},
            "description": "Open the most relevant visible search result [heuristic fallback]",
            "llm_mode": "heuristic",
            "llm_model": None,
        }

    if _is_target_marketplace_page(page_state, constraints):
        if constraints.wants_add_to_cart and current_target:
            if "корзин" in text and any(word in text for word in ("добав", "в корзину", "buy")):
                return {
                    "action": "click_text",
                    "tool_name": "click_text",
                    "arguments": {"query": "в корзину"},
                    "description": "Click the add-to-cart control if it is visible [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            if "type_into_text" not in previous_actions or current_target not in str(history[-1].get("arguments", {}).get("text", "")):
                return {
                    "action": "type_into_text",
                    "tool_name": "type_into_text",
                    "arguments": {"query": "поиск", "text": current_target, "submit": True},
                    "description": "Search for the next requested item on the marketplace [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            return {
                "action": "click_text",
                "tool_name": "click_text",
                "arguments": {"query": current_target},
                "description": "Open a relevant product card for the requested item [heuristic fallback]",
                "llm_mode": "heuristic",
                "llm_model": None,
            }

        if constraints.wants_collection and current_target:
            if "type_into_text" not in previous_actions or current_target not in str(history[-1].get("arguments", {}).get("text", "")):
                return {
                    "action": "type_into_text",
                    "tool_name": "type_into_text",
                    "arguments": {"query": "поиск", "text": current_target, "submit": True},
                    "description": "Search for the next clothing category in the requested look [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            return {
                "action": "click_text",
                "tool_name": "click_text",
                "arguments": {"query": current_target},
                "description": "Open a relevant item for the current clothing category [heuristic fallback]",
                "llm_mode": "heuristic",
                "llm_model": None,
            }

        if constraints.product_query:
            if "type_into_text" not in previous_actions:
                return {
                    "action": "type_into_text",
                    "tool_name": "type_into_text",
                    "arguments": {"query": "поиск", "text": constraints.product_query, "submit": True},
                    "description": "Search for the requested product on the marketplace [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            if last_action == "type_into_text":
                return {
                    "action": "find_elements",
                    "tool_name": "find_elements",
                    "arguments": {"query": constraints.product_query, "limit": 5},
                    "description": "Inspect visible marketplace results for the product query [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            if last_action == "find_elements":
                last_outcome = (history[-1].get("outcome") or "").lower() if history else ""
                if "no visible interactive elements matched" in last_outcome:
                    return {
                        "action": "scroll",
                        "tool_name": "scroll",
                        "arguments": {"direction": "down", "amount": 700},
                        "description": "Scroll the marketplace results and keep browsing [heuristic fallback]",
                        "llm_mode": "heuristic",
                        "llm_model": None,
                    }
            if last_action == "scroll":
                return {
                    "action": "find_elements",
                    "tool_name": "find_elements",
                    "arguments": {"query": constraints.product_query, "limit": 5},
                    "description": "Re-check marketplace results after scrolling [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            if _recent_repeated_tool(history, "click_text", constraints.product_query, threshold=2):
                return {
                    "action": "scroll",
                    "tool_name": "scroll",
                    "arguments": {"direction": "down", "amount": 700},
                    "description": "Avoid repeating the same click and reveal more marketplace results [heuristic fallback]",
                    "llm_mode": "heuristic",
                    "llm_model": None,
                }
            return {
                "action": "click_text",
                "tool_name": "click_text",
                "arguments": {"query": constraints.product_query},
                "description": "Open a relevant product result on the marketplace [heuristic fallback]",
                "llm_mode": "heuristic",
                "llm_model": None,
            }

    if elements:
        return {
            "action": "find_elements",
            "tool_name": "find_elements",
            "arguments": {"query": current_target, "limit": 5},
            "description": "Inspect visible interactive elements related to the user goal [heuristic fallback]",
            "llm_mode": "heuristic",
            "llm_model": None,
        }

    return {
        "action": "scroll",
        "tool_name": "scroll",
        "arguments": {"direction": "down", "amount": 500},
        "description": "Scroll to reveal more of the page [heuristic fallback]",
        "llm_mode": "heuristic",
        "llm_model": None,
    }


def _select_next_action(
    task: str,
    constraints: TaskConstraints,
    screenshot: str,
    history: list[dict[str, Any]],
    page_state: dict[str, Any],
    audit_log: list[str],
    llm_models_used: list[str],
) -> dict[str, Any]:
    bootstrap = _minimal_bootstrap_action(task, page_state, constraints)
    if bootstrap:
        _log(audit_log, "Started from blank page, used minimal bootstrap goto")
        return _normalize_action(bootstrap)

    action = ask_llm(
        task,
        screenshot,
        history,
        page_state,
        planning_context={
            "current_goal": (
                f"Move toward shopping goal for '{constraints.product_query or task}'"
                if constraints.query_type == "shopping"
                else "Choose the next MCP tool that moves the browser toward the user goal"
            ),
            "task_breakdown": constraints.task_breakdown or [
                "Observe the page",
                "Choose one MCP tool",
                "Execute it",
                "Verify the page changed",
            ],
            "is_long_task": constraints.is_long_task or len(history) >= 3,
            "intent": constraints.intent,
            "target_marketplace": constraints.target_marketplace,
            "product_query": constraints.product_query,
            "shopping_items": constraints.shopping_items,
            "wants_add_to_cart": constraints.wants_add_to_cart,
            "wants_collection": constraints.wants_collection,
            "max_price": constraints.max_price,
        },
    )

    if action.get("action") == "error" and (
        "No LLM response" in action.get("description", "")
        or "Failed to parse model response" in action.get("description", "")
        or "LLM in cooldown" in action.get("description", "")
        or "LLM step budget exhausted" in action.get("description", "")
    ):
        fallback = _heuristic_fallback_action(task, constraints, page_state, history)
        _log(audit_log, f"LLM unavailable, used fallback action: {action.get('description', '')}")
        return _normalize_action(fallback)

    if action.get("llm_model") and action["llm_model"] not in llm_models_used:
        llm_models_used.append(action["llm_model"])
    return _normalize_action(action)


def _get_page_state() -> dict[str, Any]:
    result = _mcp_client.call_tool("get_page_state", {})
    return result.get("page_state", {})


def _get_screenshot() -> str:
    result = _mcp_client.call_tool("screenshot", {})
    return result.get("image", "")


def _generic_summary(task: str, history: list[dict[str, Any]], page_state: dict[str, Any]) -> str:
    url = page_state.get("url", "")
    last_outcome = history[-1].get("outcome", "") if history else ""
    if url:
        return f"Задача: {task}. Текущая страница: {url}. Последний результат: {last_outcome or 'действия еще не зафиксированы'}"
    return f"Задача: {task}. Последний результат: {last_outcome or 'действия еще не зафиксированы'}"


def _build_runtime_report(
    task: str,
    constraints: TaskConstraints,
    plan: list[PlanStep],
    audit_log: list[str],
    history: list[dict[str, Any]],
    page_state: dict[str, Any],
) -> FinalReport:
    url = page_state.get("url") or ""
    title = page_state.get("title") or ""
    sources = []
    if url:
        sources.append(SourceRecord(kind="page", title=title or url, url=url, snippet=(page_state.get("text") or "")[:280]))
    for step in history:
        args = step.get("arguments", {}) or {}
        step_url = args.get("url")
        if step_url and isinstance(step_url, str) and step_url.startswith("http") and all(source.url != step_url for source in sources):
            sources.append(SourceRecord(kind="visited", title=step.get("description") or step_url, url=step_url))

    summary = _generic_summary(task, history, page_state)
    if constraints.query_type == "shopping":
        progress = _shopping_progress(constraints, history)
        marketplace = constraints.target_marketplace or "marketplace"
        if constraints.wants_collection and constraints.shopping_items:
            summary = (
                f"Задача на подбор образа на площадке {marketplace}. "
                f"Уже обработанные категории: {', '.join(progress['completed']) if progress['completed'] else 'пока нет'}. "
                f"Осталось категорий: {', '.join(progress['remaining']) if progress['remaining'] else 'не осталось'}. "
                f"Текущая страница: {url or 'неизвестно'}."
            )
        elif constraints.wants_add_to_cart and constraints.shopping_items:
            summary = (
                f"Задача на сбор корзины на площадке {marketplace}. "
                f"Уже обработанные товары: {', '.join(progress['completed']) if progress['completed'] else 'пока нет'}. "
                f"Осталось товаров: {', '.join(progress['remaining']) if progress['remaining'] else 'не осталось'}. "
                f"Текущая страница: {url or 'неизвестно'}."
            )
        elif constraints.product_query:
            summary = (
                f"Поиск товара '{constraints.product_query}'"
                f"{' на площадке ' + marketplace if constraints.target_marketplace else ''}. "
                f"Текущая страница: {url or 'неизвестно'}. "
                f"Последний результат: {history[-1].get('outcome', '') if history else 'действия еще не зафиксированы'}"
            )
    return FinalReport(
        summary=summary,
        sources=sources,
        audit_log=audit_log + [f"{item.id}:{item.status}" for item in plan],
        constraints=constraints,
        completed=bool(history),
    )


def _is_stuck_loop(history: list[dict[str, Any]]) -> bool:
    if len(history) < 3:
        return False
    recent = history[-3:]
    tool_names = [item.get("tool_name") for item in recent]
    if len(set(tool_names)) != 1:
        return False
    if tool_names[0] not in {"click_text", "find_elements", "scroll"}:
        return False
    outcomes = [str(item.get("outcome", "")).lower() for item in recent]
    return all("did not change much" in outcome or "tool execution error" in outcome for outcome in outcomes)


def _should_stop(action: dict[str, Any], step_num: int, verification: str, history: list[dict[str, Any]]) -> bool:
    if action.get("action") in {"done", "error"}:
        return True
    if step_num >= MAX_STEPS:
        return True
    if step_num >= 8 and "did not change much" in verification.lower():
        return True
    if _is_stuck_loop(history):
        return True
    return False


def _format_runtime_error(traceback_text: str) -> str:
    lower_trace = traceback_text.lower()
    if "permissionerror: [winerror 5]" in lower_trace and "playwright" in lower_trace:
        return (
            "Browser startup failed: Playwright could not launch its helper process on this Windows setup "
            "(WinError 5: Access denied).\n\n"
            f"Critical error: {traceback_text}"
        )
    return f"Critical error: {traceback_text}"


def run_browser_task(task: str, step_callback=None) -> dict[str, Any]:
    heuristic_constraints = _runtime_constraints(task)
    constraints = _merge_constraints(plan_task(task), heuristic_constraints)
    plan = _new_plan()
    steps: list[AgentStep] = []
    history: list[dict[str, Any]] = []
    audit_log: list[str] = []
    llm_models_used: list[str] = []

    try:
        _prepare_runtime()
        _log(audit_log, f"Task received: {task}")
        _log(audit_log, f"Runtime constraints: {json.dumps(constraints.model_dump(), ensure_ascii=False)}")

        final_page_state: dict[str, Any] = {}
        for step_num in range(1, MAX_STEPS + 1):
            _set_plan(plan, "observe", "in_progress")
            page_state_before = _get_page_state()
            screenshot = _get_screenshot()
            _set_plan(plan, "observe", "completed", f"Observed {len(page_state_before.get('elements', []))} visible elements")

            _set_plan(plan, "reason", "in_progress")
            action = _select_next_action(
                task=task,
                constraints=constraints,
                screenshot=screenshot,
                history=history,
                page_state=page_state_before,
                audit_log=audit_log,
                llm_models_used=llm_models_used,
            )
            _set_plan(plan, "reason", "completed", action.get("description", ""))

            step = AgentStep(
                step=step_num,
                action=action.get("action", "unknown"),
                description=action.get("description", ""),
                screenshot=screenshot,
                tool_name=action.get("tool_name"),
                tool_arguments=action.get("arguments", {}),
                llm_model=action.get("llm_model"),
                llm_mode=action.get("llm_mode"),
            )
            steps.append(step)

            _set_plan(plan, "act", "in_progress", f"Executing {step.tool_name or step.action}")
            action_failed = False
            try:
                outcome = _execute_action(action)
            except Exception as exc:
                action_failed = True
                outcome = f"Tool execution error: {exc}"
            _set_plan(plan, "act", "completed", outcome)

            _set_plan(plan, "verify", "in_progress")
            page_state_after = _get_page_state()
            final_page_state = page_state_after
            _, verification = _local_state_delta(page_state_before, page_state_after)
            _set_plan(plan, "verify", "completed", verification)

            combined_outcome = f"{outcome}. Verification: {verification}"
            step.outcome = combined_outcome
            step.verification = verification
            _record_history(history, step_num, action, combined_outcome)
            _log(
                audit_log,
                f"Step {step_num}: action={step.action}, tool={step.tool_name}, "
                f"llm_mode={step.llm_mode}, llm_model={step.llm_model} -> {combined_outcome}",
            )
            _emit_step(step_callback, step)

            if action_failed and step.tool_name not in {"click_text", "find_elements", "click_first_search_result"}:
                break

            if _should_stop(action, step_num, verification, history):
                break

        _set_plan(plan, "report", "in_progress")
        report = _build_runtime_report(task, constraints, plan, audit_log, history, final_page_state)
        _set_plan(plan, "report", "completed", "Final report assembled")

        success = bool(steps)
        final_url = final_page_state.get("url", "")
        result = {
            "success": success,
            "result": report.summary,
            "steps": steps,
            "error": None if success else "No steps were executed",
            "plan": plan,
            "report": report,
            "metadata": {
                "constraints": constraints.model_dump(),
                "intent": constraints.intent,
                "sensitive_action": constraints.sensitive_action,
                "audit_log_entries": len(audit_log),
                "llm_models_used": llm_models_used,
                "browser_left_open": _should_leave_browser_open(),
                "mcp_style_tools": True,
                "tool_loop_mode": "llm->mcp_client->mcp_server->tool",
                "mcp_tools_available": [tool.get("name") for tool in _mcp_client.list_tools()],
                "mcp_transport": _mcp_client._transport,
                "mcp_transport_note": _mcp_client._init_error,
                "current_url": final_url,
                "final_url": final_url,
                "runtime_mode": "llm_primary",
                "stuck_loop_detected": _is_stuck_loop(history),
                "executed_steps": len(steps),
            },
        }
        if step_callback:
            step_callback({"type": "done" if success else "error", **result})
        return result

    except Exception:
        error_details = traceback.format_exc()
        report = _build_runtime_report(task, constraints, plan, audit_log, history, {})
        result = {
            "success": False,
            "result": "",
            "error": _format_runtime_error(error_details),
            "steps": steps,
            "plan": plan,
            "report": report,
            "metadata": {
                "constraints": constraints.model_dump(),
                "intent": constraints.intent,
                "sensitive_action": constraints.sensitive_action,
                "audit_log_entries": len(audit_log),
                "llm_models_used": llm_models_used,
                "mcp_style_tools": True,
                "tool_loop_mode": "llm->mcp_client->mcp_server->tool",
                "mcp_transport": _mcp_client._transport,
                "mcp_transport_note": _mcp_client._init_error,
                "current_url": "",
                "final_url": "",
                "runtime_mode": "llm_primary",
                "stuck_loop_detected": _is_stuck_loop(history),
                "executed_steps": len(steps),
            },
        }
        if step_callback:
            step_callback({"type": "error", **result})
        return result


async def run_agent(task: str) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, run_browser_task, task, None)


async def run_agent_stream(task: str) -> AsyncGenerator[dict[str, Any], None]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def callback(event: dict[str, Any]):
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    def run():
        run_browser_task(task, step_callback=callback)
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    executor.submit(run)

    while True:
        event = await queue.get()
        if event is None:
            break
        yield event
