import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from browser.mcp_tools import TOOL_CATALOG
from models import TaskConstraints

_project_root = Path(__file__).resolve().parent.parent
_env_file = _project_root / ".env"

if _env_file.exists():
    with open(_env_file, "r", encoding="utf-8") as env_handle:
        for raw_line in env_handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

HF_API_KEY = os.getenv("HF_API_KEY")
DEFAULT_TEXT_MODELS = [
    "deepseek-ai/DeepSeek-V3-0324",
    "Qwen/Qwen2.5-72B-Instruct",
    "Qwen/Qwen3-32B",
]
TEXT_MODELS = [item.strip() for item in os.getenv("HF_TEXT_MODELS", "").split(",") if item.strip()] or DEFAULT_TEXT_MODELS
LLM_FAILURE_COOLDOWN_SECONDS = int(os.getenv("LLM_FAILURE_COOLDOWN_SECONDS", "60"))
MAX_LLM_STEPS_PER_RUN = int(os.getenv("MAX_LLM_STEPS_PER_RUN", "12"))

_llm_blocked_until = 0.0
_llm_block_reason = ""
_tool_by_name = {tool["name"]: tool for tool in TOOL_CATALOG}


def _tool_contract_prompt() -> str:
    lines = []
    for tool in TOOL_CATALOG:
        schema = ", ".join(f"{key}:{value}" for key, value in tool["input_schema"].items()) or "no args"
        lines.append(f"- {tool['name']}: {tool['description']} | args: {schema}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""
You are a browser automation agent that controls a browser through MCP-style tools.
You must return exactly one JSON object and nothing else.

Tool call format:
{{
  "type": "tool_call",
  "tool_name": "tool_name",
  "arguments": {{}},
  "description": "short reason for this step"
}}

Task completion format:
{{
  "type": "done",
  "result": "what was achieved",
  "description": "why the task is complete"
}}

Blocked format:
{{
  "type": "error",
  "description": "what is blocking the task"
}}

Available tools:
{_tool_contract_prompt()}

Rules:
- Return exactly one next step.
- Prefer high-level tools such as find_elements, click_text, type_into_text.
- Use goto only when a new URL must be opened.
- If the task targets a site, marketplace, portal, or app, work inside that site instead of searching the whole original sentence on a generic search engine.
- For marketplace tasks, prefer opening the marketplace first and then searching inside it.
- For shopping collection tasks, progress one missing category at a time.
- Do not repeat the same click_text step if the previous attempt did not meaningfully change the page.
- After typing in a site search field, prefer observing results with find_elements before clicking again.
- Never invent arguments that are not in the tool schema.
- If login, captcha, antibot, permissions, or a dangerous action blocks the task, return type=error.
- No markdown. No prose outside JSON.
""".strip()


PLANNER_PROMPT = """
You are a planner for a browser automation agent.
Read one user task and infer the browsing intent, target site, internal search query, and a short execution plan.
Return exactly one JSON object.

Schema:
{
  "intent": "generic | shopping | shopping_collection | navigation | information",
  "target_site_name": "site name or null",
  "target_url": "https://..." or null,
  "search_query": "what should be searched inside the site or on the web",
  "product_query": "main product query or null",
  "shopping_items": ["item1", "item2"],
  "wants_add_to_cart": true,
  "wants_collection": false,
  "max_price": 70000,
  "is_long_task": true,
  "task_breakdown": ["step 1", "step 2", "step 3"]
}

Rules:
- If the user names a site or marketplace, set target_site_name and target_url.
- If the user says "marketplace" but does not name one, you may choose a suitable popular marketplace.
- If the task is a shopping outfit or collection, populate shopping_items with clothing categories.
- If the task is about cart building, set wants_add_to_cart=true.
- If the task is long, provide 4-6 concise steps.
- Return JSON only.
""".strip()


def _set_llm_cooldown(reason: str):
    global _llm_blocked_until, _llm_block_reason
    _llm_blocked_until = time.time() + LLM_FAILURE_COOLDOWN_SECONDS
    _llm_block_reason = reason


def _get_llm_cooldown_error() -> dict[str, Any] | None:
    if time.time() < _llm_blocked_until:
        seconds_left = int(_llm_blocked_until - time.time())
        return {
            "action": "error",
            "tool_name": "error",
            "llm_mode": "cooldown",
            "description": f"LLM in cooldown. Retry in {seconds_left}s. Reason: {_llm_block_reason}",
        }
    return None


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.replace("```json", "").replace("```", "").replace("Assistant:", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass

    return {"type": "error", "description": f"Failed to parse model response: {cleaned[:200]}"}


def call_model(model: str, messages: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    if not HF_API_KEY:
        return None, "HF_API_KEY is missing"

    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {HF_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 500,
                    "stream": False,
                },
            )

        if response.status_code >= 400:
            try:
                payload = response.json()
                error_msg = payload.get("error", {}).get("message") or payload.get("error") or f"HTTP {response.status_code}"
            except Exception:
                error_msg = f"HTTP {response.status_code}"
            full_error = f"{model}: {error_msg}"
            if response.status_code in {401, 402, 403, 429}:
                _set_llm_cooldown(full_error)
            return None, full_error

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return None, f"{model}: empty response"
        content = (choices[0].get("message") or {}).get("content", "")
        if not content:
            return None, f"{model}: empty content"
        return content.strip(), None
    except Exception as exc:
        message = f"{model}: {type(exc).__name__}: {exc}"
        _set_llm_cooldown(message)
        return None, message


def _format_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return "No previous steps."
    lines = []
    for item in history[-6:]:
        lines.append(
            f"{item.get('step')}. action={item.get('action')} tool={item.get('tool_name')} "
            f"outcome={item.get('outcome', '')}"
        )
    return "\n".join(lines)


def _summarize_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for element in elements[:12]:
        summary.append(
            {
                "index": element.get("index"),
                "tag": element.get("tag"),
                "role": element.get("role"),
                "type": element.get("type"),
                "text": (element.get("text") or "")[:80],
                "placeholder": (element.get("placeholder") or "")[:60],
                "ariaLabel": (element.get("ariaLabel") or "")[:60],
                "href": (element.get("href") or "")[:120],
            }
        )
    return summary


def _build_context(
    task: str,
    page_state: dict[str, Any],
    history: list[dict[str, Any]],
    planning_context: dict[str, Any] | None = None,
) -> str:
    planning_context = planning_context or {}
    trimmed_state = {
        "url": page_state.get("url", ""),
        "title": page_state.get("title", ""),
        "text_excerpt": page_state.get("text", "")[:1800],
        "elements": _summarize_elements(page_state.get("elements", []) or []),
    }

    guidance_parts = []
    for key in (
        "current_goal",
        "intent",
        "target_marketplace",
        "product_query",
        "max_price",
    ):
        value = planning_context.get(key)
        if value not in (None, "", []):
            guidance_parts.append(f"{key}={value}")
    if planning_context.get("task_breakdown"):
        guidance_parts.append(f"task_breakdown={json.dumps(planning_context['task_breakdown'], ensure_ascii=False)}")
    if planning_context.get("shopping_items"):
        guidance_parts.append(f"shopping_items={json.dumps(planning_context['shopping_items'], ensure_ascii=False)}")
    if planning_context.get("is_long_task") is not None:
        guidance_parts.append(f"is_long_task={str(bool(planning_context['is_long_task'])).lower()}")
    if planning_context.get("wants_add_to_cart") is not None:
        guidance_parts.append(f"wants_add_to_cart={str(bool(planning_context['wants_add_to_cart'])).lower()}")
    if planning_context.get("wants_collection") is not None:
        guidance_parts.append(f"wants_collection={str(bool(planning_context['wants_collection'])).lower()}")

    return (
        f"User task:\n{task}\n\n"
        f"Planning context:\n{'; '.join(guidance_parts) if guidance_parts else 'none'}\n\n"
        f"Recent history:\n{_format_history(history)}\n\n"
        f"Page state:\n{json.dumps(trimmed_state, ensure_ascii=False)}\n"
    )


def _tool_name_to_action(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name in {
        "goto",
        "find_elements",
        "click_text",
        "click_first_search_result",
        "type_into_text",
        "hover",
        "press_key",
        "select_option",
        "scroll",
        "wait_for_text",
    }:
        return tool_name
    if tool_name == "navigate_history":
        return "forward" if arguments.get("direction") == "forward" else "back"
    if tool_name == "click":
        if arguments.get("index") is not None:
            return "click_element"
        if arguments.get("selector"):
            return "click_selector"
        return "click_xy"
    if tool_name == "type_text":
        if arguments.get("index") is not None:
            return "type_element"
        if arguments.get("selector"):
            return "type_selector"
        return "type_xy"
    return tool_name


def _validate_arguments(tool_name: str, arguments: dict[str, Any]) -> str | None:
    tool = _tool_by_name.get(tool_name)
    if not tool:
        return f"Unknown tool: {tool_name}"
    required = [key for key, value in tool["input_schema"].items() if not value.endswith("?")]
    for key in required:
        if key not in arguments or arguments.get(key) in {None, ""}:
            return f"Missing required argument '{key}' for tool {tool_name}"
    return None


def normalize_action(raw: dict[str, Any]) -> dict[str, Any]:
    response_type = raw.get("type")
    if response_type == "done":
        return {
            "action": "done",
            "tool_name": "done",
            "description": raw.get("description", "Task completed"),
            "result": raw.get("result", "Task completed"),
        }
    if response_type == "error":
        return {
            "action": "error",
            "tool_name": "error",
            "description": raw.get("description", "Agent reported an error"),
        }

    tool_name = raw.get("tool_name")
    arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
    if not tool_name:
        return {
            "action": "error",
            "tool_name": "error",
            "description": "Model response does not contain tool_name",
        }

    validation_error = _validate_arguments(tool_name, arguments)
    if validation_error:
        return {
            "action": "error",
            "tool_name": "error",
            "description": validation_error,
        }

    return {
        "action": _tool_name_to_action(tool_name, arguments),
        "tool_name": tool_name,
        "arguments": arguments,
        "description": raw.get("description", f"Call tool {tool_name}"),
    }


def _query_models(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    errors = []
    for model in TEXT_MODELS:
        text, error = call_model(model, messages)
        if error:
            errors.append(error)
            continue
        return extract_json(text or ""), model, None
    return None, None, " | ".join(errors) if errors else "No LLM response"


def _clean_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if isinstance(item, str):
            clean = item.strip()
            if clean:
                result.append(clean)
    return result[:8]


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _to_float(value: Any) -> float | None:
    if value in (None, "", []):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _looks_like_shopping_task(task: str) -> bool:
    lowered = task.lower()
    markers = (
        "marketplace",
        "маркетплейс",
        "магазин",
        "корзин",
        "add to cart",
        "добавь",
        "купить",
        "buy ",
        "shopping",
        "outfit",
        "лук",
        "образ",
        "одежд",
        "price",
        "цена",
        "руб",
        "ноутбук",
        "смартфон",
        "кроссов",
        "ozon",
        "wildberries",
        "amazon",
        "яндекс маркет",
    )
    return any(marker in lowered for marker in markers)


def _extract_explicit_domain(task: str) -> str | None:
    match = re.search(r"\b(?:https?://)?([a-z0-9-]+\.[a-z]{2,})(?:/|$)", task.lower())
    return match.group(1) if match else None


def plan_task(task: str) -> TaskConstraints | None:
    if not _looks_like_shopping_task(task):
        return None
    if _get_llm_cooldown_error():
        return None

    messages = [
        {"role": "system", "content": PLANNER_PROMPT},
        {"role": "user", "content": task},
    ]
    parsed, _, error = _query_models(messages)
    if error or not parsed:
        return None

    intent = str(parsed.get("intent") or "generic").strip() or "generic"
    target_site_name = parsed.get("target_site_name")
    target_url = parsed.get("target_url")
    search_query = parsed.get("search_query") or task
    product_query = parsed.get("product_query")
    shopping_items = _clean_string_list(parsed.get("shopping_items"))
    wants_add_to_cart = _to_bool(parsed.get("wants_add_to_cart", False))
    wants_collection = _to_bool(parsed.get("wants_collection", False))
    max_price = _to_float(parsed.get("max_price"))
    task_breakdown = _clean_string_list(parsed.get("task_breakdown"))
    is_long_task = _to_bool(parsed.get("is_long_task", False) or len(task_breakdown) >= 4)

    constraints = TaskConstraints(
        query_type="shopping" if intent in {"shopping", "shopping_collection"} else "general",
        product_query=product_query,
        marketplaces=[target_site_name] if isinstance(target_site_name, str) and target_site_name.strip() else [],
        shopping_items=shopping_items,
        target_marketplace=target_site_name if isinstance(target_site_name, str) else None,
        wants_add_to_cart=wants_add_to_cart,
        wants_collection=wants_collection,
        max_price=max_price,
        raw_query=task,
        search_query=search_query,
        requested_url=target_url if isinstance(target_url, str) else None,
        intent=intent,
        sensitive_action=wants_add_to_cart,
        is_long_task=is_long_task,
        task_breakdown=task_breakdown,
    )
    explicit_domain = _extract_explicit_domain(task)
    if explicit_domain and constraints.requested_url and explicit_domain not in constraints.requested_url.lower():
        return None
    if not _looks_like_shopping_task(task) and constraints.query_type == "shopping":
        return None
    return constraints


def ask_llm(
    task: str,
    screenshot_base64: str,
    history: list[dict[str, Any]],
    page_state: dict[str, Any],
    planning_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cooldown_error = _get_llm_cooldown_error()
    if cooldown_error:
        return cooldown_error

    llm_steps = sum(1 for item in history if item.get("llm_mode") in {"text", "vision"})
    if llm_steps >= MAX_LLM_STEPS_PER_RUN:
        return {
            "action": "error",
            "tool_name": "error",
            "llm_mode": "budget",
            "description": "LLM step budget exhausted for this run",
        }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_context(task, page_state, history, planning_context)},
    ]

    parsed, model, error = _query_models(messages)
    if error or not parsed:
        return {
            "action": "error",
            "tool_name": "error",
            "llm_mode": "failed",
            "description": f"No LLM response. Details: {error}" if error else "No LLM response",
        }

    normalized = normalize_action(parsed)
    normalized["llm_model"] = model
    normalized["llm_mode"] = "text"
    return normalized
