import asyncio
import json
from typing import Any

from browser.browser import BrowserManager
from browser.mcp_tools import TOOL_CATALOG, execute_tool
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

app = Server("browser-agent")
browser = BrowserManager()
_started = False
_tool_by_name = {tool["name"]: tool for tool in TOOL_CATALOG}


def ensure_browser_started():
    global _started
    if not _started:
        browser.start(headless=False)
        _started = True


def _tool_requires_browser(name: str) -> bool:
    tool = _tool_by_name.get(name)
    if not tool:
        return True
    return True


def _schema_type(value: str) -> str:
    if "integer" in value:
        return "integer"
    if "boolean" in value:
        return "boolean"
    return "string"


def _tool_to_mcp(tool: dict[str, Any]) -> types.Tool:
    properties: dict[str, Any] = {}
    required: list[str] = []

    for key, value in tool["input_schema"].items():
        properties[key] = {"type": _schema_type(value)}
        if not value.endswith("?"):
            required.append(key)

    return types.Tool(
        name=tool["name"],
        description=tool["description"],
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


def _coerce_argument(value: Any, schema_value: str) -> Any:
    if value is None:
        return None
    if "integer" in schema_value:
        return int(value)
    if "boolean" in schema_value:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return value


def _normalize_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    tool = _tool_by_name.get(tool_name)
    if not tool:
        raise ValueError(f"Unknown tool: {tool_name}")

    arguments = arguments or {}
    normalized: dict[str, Any] = {}

    for key, schema_value in tool["input_schema"].items():
        if key in arguments:
            normalized[key] = _coerce_argument(arguments[key], schema_value)

    required = [key for key, value in tool["input_schema"].items() if not value.endswith("?")]
    missing = [key for key in required if key not in normalized or normalized[key] in {None, ""}]
    if missing:
        raise ValueError(f"Missing required arguments for {tool_name}: {', '.join(missing)}")

    return normalized


def _result_to_mcp_content(result: dict[str, Any]) -> list[types.TextContent | types.ImageContent]:
    if result.get("image"):
        return [
            types.ImageContent(
                type="image",
                data=result["image"],
                mimeType=result.get("mime_type", "image/png"),
            )
        ]

    return [
        types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False),
        )
    ]


def list_mcp_tools() -> list[types.Tool]:
    return [_tool_to_mcp(tool) for tool in TOOL_CATALOG]


def call_mcp_tool_sync(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    if _tool_requires_browser(name):
        ensure_browser_started()
    try:
        normalized_arguments = _normalize_arguments(name, arguments)
        result = execute_tool(browser, name, normalized_arguments)
        return types.CallToolResult(
            content=_result_to_mcp_content(result),
            structuredContent=result,
            isError=result.get("status") == "error",
        )
    except Exception as exc:
        error_payload = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        return types.CallToolResult(
            content=_result_to_mcp_content(error_payload),
            structuredContent=error_payload,
            isError=True,
        )


async def call_mcp_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    return await asyncio.to_thread(call_mcp_tool_sync, name, arguments)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return list_mcp_tools()


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    return await call_mcp_tool(name, arguments)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
