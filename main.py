import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from browser.agent import run_agent, run_agent_stream
from models import AgentResponse, UserRequest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(title="Browser AI Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_dist_dir = Path(__file__).resolve().parent / "frontend" / "dist"
frontend_build_dir = Path(__file__).resolve().parent / "frontend" / "build"

executions: dict[str, dict[str, Any]] = {}
execution_lock = asyncio.Lock()
EXECUTION_TTL_SECONDS = 60 * 60 * 6


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_step_dump(step: Any) -> dict[str, Any]:
    if hasattr(step, "model_dump"):
        return step.model_dump()
    return dict(step)


def _safe_plan_dump(plan_item: Any) -> dict[str, Any]:
    if hasattr(plan_item, "model_dump"):
        return plan_item.model_dump()
    return dict(plan_item)


def _cleanup_old_executions():
    now = datetime.now(timezone.utc)
    stale_ids = []
    for execution_id, payload in executions.items():
        updated_at = payload.get("updated_at")
        if not updated_at:
            continue
        try:
            updated = datetime.fromisoformat(updated_at)
        except Exception:
            continue
        age_seconds = (now - updated).total_seconds()
        if age_seconds > EXECUTION_TTL_SECONDS:
            stale_ids.append(execution_id)
    for execution_id in stale_ids:
        executions.pop(execution_id, None)


def _extract_final_url(agent_result: dict[str, Any]) -> str:
    report = agent_result.get("report")
    metadata = agent_result.get("metadata", {})
    steps = agent_result.get("steps", [])
    if report:
        if getattr(report, "best_product", None) and report.best_product.url:
            return report.best_product.url
        if getattr(report, "exchange_rate", None) and report.exchange_rate.source_url:
            return report.exchange_rate.source_url
        news = getattr(report, "news", None) or []
        if news and getattr(news[0], "url", ""):
            return news[0].url
        sources = getattr(report, "sources", None) or []
        if sources and getattr(sources[0], "url", ""):
            return sources[0].url
    for step in reversed(steps):
        tool_name = getattr(step, "tool_name", None)
        tool_arguments = getattr(step, "tool_arguments", {}) or {}
        if tool_name == "goto" and tool_arguments.get("url"):
            return tool_arguments["url"]
    return metadata.get("current_url") or metadata.get("final_url") or ""


def _normalize_execution_result(agent_result: dict[str, Any]) -> dict[str, Any]:
    report = agent_result.get("report")
    metadata = agent_result.get("metadata", {})
    steps = [_safe_step_dump(step) for step in agent_result.get("steps", [])]
    plan = [_safe_plan_dump(step) for step in agent_result.get("plan", [])]
    logs = list(getattr(report, "audit_log", []) or [])

    return {
        "summary": agent_result.get("result") or getattr(report, "summary", "") or "",
        "answer": agent_result.get("result") or getattr(report, "summary", "") or "",
        "message": agent_result.get("result") or getattr(report, "summary", "") or "",
        "final_url": _extract_final_url(agent_result),
        "current_url": metadata.get("current_url", "") or _extract_final_url(agent_result),
        "steps": steps,
        "history": steps,
        "logs": logs,
        "plan": plan,
        "screenshot": steps[-1].get("screenshot", "") if steps else "",
        "report": report.model_dump() if hasattr(report, "model_dump") else report,
        "metadata": metadata,
        "runtime": {
            "llm_models_used": metadata.get("llm_models_used", []),
            "mcp_transport": metadata.get("mcp_transport"),
            "tool_loop_mode": metadata.get("tool_loop_mode"),
            "browser_left_open": metadata.get("browser_left_open"),
            "runtime_mode": metadata.get("runtime_mode"),
        },
    }


async def _run_execution(execution_id: str, task: str):
    _cleanup_old_executions()
    async with execution_lock:
        executions[execution_id]["status"] = "running"
        executions[execution_id]["updated_at"] = _utc_now()

    try:
        result = await run_agent(task)
        status = "completed" if result.get("success") else "failed"
        payload = _normalize_execution_result(result)
        error = result.get("error", "")
    except Exception as exc:
        status = "error"
        payload = {}
        error = f"{type(exc).__name__}: {exc}"

    async with execution_lock:
        executions[execution_id]["status"] = status
        executions[execution_id]["updated_at"] = _utc_now()
        executions[execution_id]["error"] = error
        executions[execution_id]["result"] = payload


@app.post("/agent/run")
async def run(request: UserRequest):
    result = await run_agent(request.query)
    return AgentResponse(
        success=result["success"],
        result=result.get("result", ""),
        steps=result.get("steps", []),
        error=result.get("error"),
        plan=result.get("plan", []),
        report=result.get("report"),
        metadata=result.get("metadata", {}),
    )


@app.post("/agent/stream")
async def stream(request: UserRequest):
    async def generate():
        async for event in run_agent_stream(request.query):
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/execute")
async def execute_task(request: dict[str, Any]):
    task = (request.get("task") or request.get("user_prompt") or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="Task is required")

    execution_id = uuid.uuid4().hex
    execution = {
        "execution_id": execution_id,
        "status": "pending",
        "error": "",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "task": task,
        "result": {},
    }

    async with execution_lock:
        _cleanup_old_executions()
        executions[execution_id] = execution

    asyncio.create_task(_run_execution(execution_id, task))
    return {"execution_id": execution_id, "status": "pending"}


@app.get("/executions/{execution_id}")
async def get_execution(execution_id: str):
    async with execution_lock:
        execution = executions.get(execution_id)

    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    return {
        "execution_id": execution["execution_id"],
        "status": execution["status"],
        "error": execution["error"],
        "created_at": execution["created_at"],
        "updated_at": execution["updated_at"],
        "result": execution["result"],
    }


@app.get("/health")
async def health():
    async with execution_lock:
        _cleanup_old_executions()
        execution_items = list(executions.values())
    running = sum(1 for item in execution_items if item.get("status") in {"pending", "running"})
    return {
        "status": "ok",
        "frontend": "dist" if frontend_dist_dir.exists() else "build" if frontend_build_dir.exists() else "missing",
        "active_executions": running,
        "stored_executions": len(execution_items),
        "timestamp": _utc_now(),
    }


if frontend_dist_dir.exists():
    app.mount("/", StaticFiles(directory=frontend_dist_dir, html=True), name="frontend")
elif frontend_build_dir.exists():
    app.mount("/", StaticFiles(directory=frontend_build_dir, html=True), name="frontend")
