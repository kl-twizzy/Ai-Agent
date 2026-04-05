from typing import Any

from browser.browser import BrowserManager


TOOL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "new_tab",
        "category": "browser",
        "description": "Open a fresh browser tab and focus it.",
        "input_schema": {},
    },
    {
        "name": "goto",
        "category": "browser",
        "description": "Open a URL in the browser.",
        "input_schema": {"url": "string"},
    },
    {
        "name": "get_page_state",
        "category": "browser",
        "description": "Return URL, page title, text excerpt and visible interactive elements.",
        "input_schema": {},
    },
    {
        "name": "screenshot",
        "category": "browser",
        "description": "Capture a screenshot of the current page.",
        "input_schema": {},
    },
    {
        "name": "click",
        "category": "browser",
        "description": "Click by element index, CSS selector or coordinates.",
        "input_schema": {"index": "integer?", "selector": "string?", "x": "integer?", "y": "integer?"},
    },
    {
        "name": "hover",
        "category": "browser",
        "description": "Hover over an element by index or selector.",
        "input_schema": {"index": "integer?", "selector": "string?"},
    },
    {
        "name": "type_text",
        "category": "browser",
        "description": "Type text into an element by index, selector or coordinates.",
        "input_schema": {
            "text": "string",
            "index": "integer?",
            "selector": "string?",
            "x": "integer?",
            "y": "integer?",
            "submit": "boolean?",
        },
    },
    {
        "name": "find_elements",
        "category": "browser",
        "description": "Find the best matching interactive elements by natural-language text, label, placeholder, or role.",
        "input_schema": {"query": "string", "limit": "integer?", "action_hint": "string?"},
    },
    {
        "name": "click_text",
        "category": "browser",
        "description": "Click the best visible element matching a natural-language query.",
        "input_schema": {"query": "string"},
    },
    {
        "name": "click_first_search_result",
        "category": "browser",
        "description": "Open the first visible external result on a search results page.",
        "input_schema": {},
    },
    {
        "name": "type_into_text",
        "category": "browser",
        "description": "Type into the best matching input-like element selected by a natural-language query.",
        "input_schema": {"query": "string", "text": "string", "submit": "boolean?"},
    },
    {
        "name": "press_key",
        "category": "browser",
        "description": "Press a keyboard key such as Enter, Tab or Escape.",
        "input_schema": {"key": "string"},
    },
    {
        "name": "select_option",
        "category": "browser",
        "description": "Select an option in a dropdown by value or label.",
        "input_schema": {"value": "string", "index": "integer?", "selector": "string?", "label": "string?"},
    },
    {
        "name": "scroll",
        "category": "browser",
        "description": "Scroll the page or a hovered element.",
        "input_schema": {"direction": "up|down", "amount": "integer?", "index": "integer?", "selector": "string?"},
    },
    {
        "name": "navigate_history",
        "category": "browser",
        "description": "Navigate browser history back or forward.",
        "input_schema": {"direction": "string"},
    },
    {
        "name": "wait_for_text",
        "category": "browser",
        "description": "Wait until visible page text appears.",
        "input_schema": {"text": "string", "timeout_ms": "integer?"},
    },
]


ACTION_MAP: dict[str, str] = {
    "new_tab": "new_tab",
    "goto": "goto",
    "find_elements": "find_elements",
    "click_element": "click",
    "type_element": "type_text",
    "click_selector": "click",
    "type_selector": "type_text",
    "click_xy": "click",
    "type_xy": "type_text",
    "click_text": "click_text",
    "click_first_search_result": "click_first_search_result",
    "type_into_text": "type_into_text",
    "hover": "hover",
    "press_key": "press_key",
    "select_option": "select_option",
    "scroll": "scroll",
    "back": "navigate_history",
    "forward": "navigate_history",
    "wait_for_text": "wait_for_text",
    "wait": "wait",
    "done": "done",
    "error": "error",
}


def execute_tool(browser: BrowserManager, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "new_tab":
        browser.open_new_tab()
        return {"status": "ok", "message": "Opened a new tab"}

    if name == "goto":
        browser.goto(arguments["url"])
        return {"status": "ok", "message": f"Opened page: {arguments['url']}"}

    if name == "screenshot":
        return {"status": "ok", "image": browser.screenshot(), "mime_type": "image/png"}

    if name == "get_page_state":
        return {"status": "ok", "page_state": browser.get_page_state()}

    if name == "click":
        browser.click(
            selector=arguments.get("selector"),
            index=arguments.get("index"),
            x=arguments.get("x"),
            y=arguments.get("y"),
        )
        return {"status": "ok", "message": "Click action completed"}

    if name == "hover":
        browser.hover(
            selector=arguments.get("selector"),
            index=arguments.get("index"),
        )
        return {"status": "ok", "message": "Hover action completed"}

    if name == "type_text":
        browser.type_text(
            selector=arguments.get("selector"),
            index=arguments.get("index"),
            x=arguments.get("x"),
            y=arguments.get("y"),
            text=arguments["text"],
            submit=bool(arguments.get("submit", False)),
        )
        return {"status": "ok", "message": "Typing action completed"}

    if name == "find_elements":
        matches = browser.find_elements(
            query=arguments["query"],
            limit=int(arguments.get("limit", 5)),
            action_hint=arguments.get("action_hint"),
        )
        return {"status": "ok", "matches": matches}

    if name == "click_text":
        browser.click_best_match(arguments["query"])
        return {"status": "ok", "message": f"Clicked best matching element for query: {arguments['query']}"}

    if name == "click_first_search_result":
        browser.click_first_search_result()
        return {"status": "ok", "message": "Clicked the first visible external search result"}

    if name == "type_into_text":
        browser.type_into_best_match(
            query=arguments["query"],
            text=arguments["text"],
            submit=bool(arguments.get("submit", False)),
        )
        return {"status": "ok", "message": f"Typed into best matching field for query: {arguments['query']}"}

    if name == "press_key":
        browser.press_key(arguments["key"])
        return {"status": "ok", "message": f"Pressed key: {arguments['key']}"}

    if name == "select_option":
        browser.select_option(
            value=arguments["value"],
            selector=arguments.get("selector"),
            index=arguments.get("index"),
            label=arguments.get("label"),
        )
        return {"status": "ok", "message": "Select action completed"}

    if name == "scroll":
        browser.scroll(
            direction=arguments.get("direction", "down"),
            amount=int(arguments.get("amount", 300)),
            selector=arguments.get("selector"),
            index=arguments.get("index"),
        )
        return {"status": "ok", "message": "Scroll action completed"}

    if name == "navigate_history":
        browser.navigate_history(arguments.get("direction", "back"))
        return {"status": "ok", "message": f"Navigated {arguments.get('direction', 'back')}"}

    if name == "wait_for_text":
        browser.wait_for_text(arguments["text"], timeout_ms=int(arguments.get("timeout_ms", 10000)))
        return {"status": "ok", "message": f"Waited for text: {arguments['text']}"}

    return {"status": "error", "message": f"Unsupported tool: {name}"}
