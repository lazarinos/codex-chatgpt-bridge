"""Minimal HTTP MCP-compatible server for AI agents and model connectors."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import ToolProfile
from .gateway import BridgeError, LocalGateway

JsonObject = dict[str, Any]

_MAX_PARALLEL_WORKERS: int = max(128, (os.cpu_count() or 8) * 16)
_PARALLEL_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_PARALLEL_WORKERS,
    thread_name_prefix="bridge_worker",
)
_METRICS_LOCK = threading.Lock()
_ACTIVE_WORKERS: int = 0
_PEAK_WORKERS: int = 0
_TOTAL_CALLS: int = 0
_TURN_CALLS: int = 0


def reset_turn_calls() -> None:
    global _TURN_CALLS
    with _METRICS_LOCK:
        _TURN_CALLS = 0


def get_parallel_telemetry(gateway: LocalGateway | None = None) -> JsonObject:
    with _METRICS_LOCK:
        active_w = _ACTIVE_WORKERS
        peak_w = _PEAK_WORKERS
        total_c = _TOTAL_CALLS
        turn_c = _TURN_CALLS
    running_jobs = 0
    if gateway is not None:
        try:
            running_jobs = sum(
                1 for j in gateway._job_manager.list_jobs() if j.get("status") == "running"
            )
        except Exception:
            running_jobs = 0
    return {
        "active_workers": active_w,
        "peak_workers": peak_w,
        "max_workers": _MAX_PARALLEL_WORKERS,
        "turn_calls": turn_c,
        "total_calls": total_c,
        "running_jobs": running_jobs,
    }


def _run_handler_in_worker_thread(
    handler: Callable[..., Awaitable[JsonObject]],
    arguments: JsonObject,
) -> JsonObject:
    return asyncio.run(handler(**arguments))


_THREAD_OFFLOAD_TOOLS: set[str] = {
    "search_files",
    "list_dir",
    "write_file",
    "edit_file",
    "file_ops",
    "view_image",
    "system_info",
}


class JsonRpcRequest(BaseModel):
    """A JSON-RPC request used by MCP over HTTP."""

    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: JsonObject = Field(default_factory=dict)


class LiveInboxMessageRequest(BaseModel):
    """Payload for sending a live user message or agent reply via local HTTP."""

    text: str
    priority: str = "normal"


def create_app(gateway: LocalGateway) -> FastAPI:
    """Create the FastAPI app."""

    app = FastAPI(title="Bridge MCP Server", version="0.5.0")

    @app.get("/healthz")
    async def healthz() -> JsonObject:
        return {
            "status": "ok",
            "server": "bridge",
            "version": "0.5.0",
            "unread_user_messages": len(gateway._inbox_store.peek_unread_for_agent()),
            "parallel_engine": get_parallel_telemetry(gateway),
        }

    @app.get("/inbox")
    async def inbox_get(limit: int = 50) -> JsonObject:
        state = await asyncio.get_running_loop().run_in_executor(
            _PARALLEL_POOL, lambda: gateway.inbox_state(limit=limit)
        )
        state["parallel_engine"] = get_parallel_telemetry(gateway)
        try:
            state["active_jobs"] = [
                {
                    "job_id": j["job_id"],
                    "status": j["status"],
                    "pid": j.get("pid"),
                    "elapsed_s": j.get("elapsed_s"),
                    "command": str(j.get("command", ""))[:80],
                }
                for j in gateway._job_manager.list_jobs()
                if j.get("status") == "running"
            ]
        except Exception:
            state["active_jobs"] = []
        return state

    @app.post("/inbox/send")
    async def inbox_send(payload: LiveInboxMessageRequest) -> JsonObject:
        reset_turn_calls()
        msg = gateway.inbox_send_user_message(payload.text, priority=payload.priority)
        return {"status": "queued", "message": msg, "inbox": gateway.inbox_state(limit=25)}

    @app.post("/inbox/reply")
    async def inbox_reply(payload: LiveInboxMessageRequest) -> JsonObject:
        msg = gateway.inbox_send_agent_reply(payload.text)
        return {"status": "sent", "message": msg, "inbox": gateway.inbox_state(limit=25)}

    @app.post("/inbox/clear")
    async def inbox_clear() -> JsonObject:
        return gateway.inbox_clear()

    @app.post("/mcp")
    async def mcp_endpoint(
        request: JsonRpcRequest,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        auth_error = _auth_error(gateway.config.auth_token, authorization)
        if auth_error is not None:
            return auth_error
        return await _handle_mcp_request(gateway, request, auth_mode="bearer")

    @app.post("/mcp/{connector_secret}")
    async def mcp_connector_secret_endpoint(
        connector_secret: str,
        request: JsonRpcRequest,
    ) -> JSONResponse:
        if connector_secret != gateway.config.connector_secret:
            return _auth_error(gateway.config.connector_secret, None)
        return await _handle_mcp_request(gateway, request, auth_mode="connector_secret")

    return app


async def call_tool(gateway: LocalGateway, params: JsonObject) -> JsonObject:
    """Dispatch a tool call."""

    name = str(params.get("name", ""))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("tools/call params.arguments must be an object")
    handlers: dict[str, Callable[..., Awaitable[JsonObject]]] = {
        "bridge_status": gateway.bridge_status,
        "set_bridge_mode": gateway.set_bridge_mode,
        "grant_path": gateway.grant_path,
        "revoke_grant": gateway.revoke_grant,
        "list_grants": gateway.list_grants,
        "search_files": gateway.search_files,
        "read_file": gateway.read_file,
        "view_image": gateway.view_image,
        "write_file": gateway.write_file,
        "list_dir": gateway.list_dir,
        "edit_file": gateway.edit_file,
        "file_ops": gateway.file_ops,
        "exec_command": gateway.exec_command,
        "process_manager": gateway.process_manager,
        "system_info": gateway.system_info,
    }
    if gateway.config.enable_codex_tasks:
        handlers["bridge_task_run"] = gateway.bridge_task_run
        handlers["bridge_delegate"] = gateway.bridge_delegate
        handlers["bridge_session_start"] = gateway.bridge_session_start
        handlers["bridge_session_continue"] = gateway.bridge_session_continue
        handlers["bridge_session_list"] = gateway.bridge_session_list
        handlers["bridge_session_status"] = gateway.bridge_session_status
    visible_names = visible_tool_names(
        gateway.config.enable_codex_tasks,
        tool_profile=gateway.config.tool_profile,
    )
    handlers = {
        tool_name: handler for tool_name, handler in handlers.items() if tool_name in visible_names
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"unknown tool: {name}")
    arg_preview = (
        str(arguments.get("path") or arguments.get("command") or arguments.get("action") or "")[:160]
    )
    try:
        gateway.inbox_record_activity(name, arg_preview)
    except Exception:
        pass

    global _ACTIVE_WORKERS, _PEAK_WORKERS, _TOTAL_CALLS, _TURN_CALLS
    with _METRICS_LOCK:
        _ACTIVE_WORKERS += 1
        _TOTAL_CALLS += 1
        _TURN_CALLS += 1
        if _ACTIVE_WORKERS > _PEAK_WORKERS:
            _PEAK_WORKERS = _ACTIVE_WORKERS

    try:
        structured = await handler(**arguments)
    finally:
        with _METRICS_LOCK:
            _ACTIVE_WORKERS = max(0, _ACTIVE_WORKERS - 1)
    mcp_image: JsonObject | None = None
    if isinstance(structured, dict):
        maybe_img = structured.pop("_mcp_image", None)
        if isinstance(maybe_img, dict) and "data" in maybe_img:
            mcp_image = maybe_img
        structured["bridge_chat_console"] = "online"
        try:
            unread = gateway.inbox_pop_unread(tool_name=name)
        except Exception:
            unread = []
        if unread:
            structured["user_messages"] = unread
        jobs_info = gateway._job_manager.list_jobs()
        active_jobs = [
            {"job_id": j["job_id"], "status": j["status"], "elapsed_s": j.get("elapsed_s")}
            for j in jobs_info
            if j.get("status") == "running"
        ]
        if active_jobs:
            structured["active_jobs"] = active_jobs
        completed_alerts = gateway._job_manager.pop_newly_completed_jobs()
        if completed_alerts:
            structured["completed_jobs_alert"] = completed_alerts
    content_blocks: list[JsonObject] = [
        {"type": "text", "text": json.dumps(structured, ensure_ascii=False, indent=2)}
    ]
    if mcp_image is not None:
        content_blocks.append(
            {
                "type": "image",
                "data": mcp_image["data"],
                "mimeType": mcp_image.get("mimeType", "image/png"),
            }
        )
    return {
        "structuredContent": structured,
        "content": content_blocks,
    }


async def _handle_mcp_request(
    gateway: LocalGateway,
    request: JsonRpcRequest,
    *,
    auth_mode: str,
) -> JSONResponse:
    try:
        if request.method == "tools/list":
            result = {
                "tools": tool_definitions(
                    gateway.config.enable_codex_tasks,
                    auth_mode=auth_mode,
                    tool_profile=gateway.config.tool_profile,
                )
            }
        elif request.method == "tools/call":
            result = await call_tool(gateway, request.params)
        elif request.method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bridge", "version": "0.5.0"},
            }
        elif request.method in {"notifications/initialized", "ping"}:
            result = {}
        else:
            return _jsonrpc_error(request.id, -32601, f"unknown method: {request.method}")
    except BridgeError as exc:
        return _jsonrpc_error(request.id, -32000, str(exc))
    except ValueError as exc:
        return _jsonrpc_error(request.id, -32602, str(exc))
    return JSONResponse({"jsonrpc": "2.0", "id": request.id, "result": result})


def tool_definitions(
    enable_codex_tasks: bool,
    *,
    auth_mode: str = "bearer",
    tool_profile: ToolProfile = "full",
) -> list[JsonObject]:
    """Return tool schemas visible to AI agents."""

    tools: list[JsonObject] = [
        {
            "name": "bridge_status",
            "title": "Bridge status",
            "description": (
                "Check whether the local bridge is alive and which capabilities are available."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "set_bridge_mode",
            "title": "Set bridge trust mode",
            "description": (
                "Switch the local bridge between restricted grants and full delegate mode. "
                "full_delegate gives this connector broad local read, write, and execute access."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["restricted", "full_delegate"],
                    }
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "grant_path",
            "title": "Grant local path",
            "description": (
                "Add or replace a persisted local path grant for reading, writing, and execution."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "name": {"type": ["string", "null"], "default": None},
                    "read": {"type": "boolean", "default": True},
                    "write": {"type": "boolean", "default": False},
                    "execute": {"type": "boolean", "default": False},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "revoke_grant",
            "title": "Revoke local path grant",
            "description": "Remove a persisted local path grant by path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "list_grants",
            "title": "List local grants",
            "description": (
                "List user-approved local directories and their read, write, "
                "and execute permissions."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "search_files",
            "title": "Search approved local files",
            "description": (
                "Search text or filenames (prefix with 'files:' or 'glob:') in local directories "
                "using ripgrep or built-in Python fallback."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 240},
                    "root": {"type": ["string", "null"], "default": None},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "read_file",
            "title": "Read approved local file",
            "description": (
                "Read a UTF-8 text file (supports #L10-L50 line ranges or directory listing if "
                "path is a folder). In full_delegate mode, also supports 'ps:<cmd>', 'cmd:<cmd>', "
                "'py:<code>', 'bg:<cmd>', 'jobs:list', 'jobs:status:<id>', 'jobs:kill:<id>', "
                "'img:<path>', 'screenshot:', and 'sysinfo:' shortcuts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "max_chars": {"type": ["integer", "null"], "minimum": 1, "default": None},
                    "start_line": {"type": ["integer", "null"], "minimum": 1, "default": None},
                    "end_line": {"type": ["integer", "null"], "minimum": 1, "default": None},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "view_image",
            "title": "View local image or capture live screenshot",
            "description": (
                "Read a local image file (.png, .jpg, .webp, .bmp) or pass path='screenshot' "
                "to capture a live screen/game frame, returning native MCP ImageContent plus "
                "visual dimensions and RGB statistics."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "max_width": {
                        "type": "integer",
                        "minimum": 64,
                        "maximum": 4096,
                        "default": 1280,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "write_file",
            "title": "Write approved local file",
            "description": (
                "Write UTF-8 text to a file (or use path='edit:<filepath>' with "
                "<<<OLD\\n...\\n===\\n...\\n>>> content for fast surgical replacement)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "content": {"type": "string", "maxLength": 10000000},
                    "create_parents": {"type": "boolean", "default": False},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "list_dir",
            "title": "List directory contents",
            "description": (
                "List files and subdirectories in a local folder with sizes and timestamps."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "~", "maxLength": 1000},
                    "recursive": {"type": "boolean", "default": False},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 10, "default": 2},
                    "pattern": {"type": ["string", "null"], "default": None},
                    "include_hidden": {"type": "boolean", "default": False},
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 200,
                    },
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "edit_file",
            "title": "Surgical file edit",
            "description": (
                "Replace exact text blocks, line ranges, or append/prepend text in a file "
                "without transferring the entire file over the network."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "old_text": {"type": ["string", "null"], "default": None},
                    "new_text": {"type": "string", "default": ""},
                    "replace_all": {"type": "boolean", "default": False},
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "replace_lines", "append", "prepend"],
                        "default": "replace",
                    },
                    "start_line": {"type": ["integer", "null"], "minimum": 1, "default": None},
                    "end_line": {"type": ["integer", "null"], "minimum": 1, "default": None},
                    "create_if_missing": {"type": "boolean", "default": False},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "file_ops",
            "title": "Filesystem operations",
            "description": (
                "Perform fast filesystem operations: stat, mkdir, delete, move, copy, "
                "read_lines, head, or tail."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "stat",
                            "mkdir",
                            "delete",
                            "move",
                            "copy",
                            "read_lines",
                            "head",
                            "tail",
                        ],
                    },
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "dest_path": {"type": ["string", "null"], "default": None},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "end_line": {"type": "integer", "minimum": 1, "default": 200},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 100},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["action", "path"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "exec_command",
            "title": "Execute local command",
            "description": (
                "Execute a PowerShell, CMD, Python, or shell command directly on the local "
                "computer with hang protection and automatic backgrounding for long tasks."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 50000},
                    "cwd": {"type": ["string", "null"], "default": None},
                    "shell": {
                        "type": "string",
                        "enum": ["auto", "powershell", "cmd", "python", "sh"],
                        "default": "auto",
                    },
                    "timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 600,
                        "default": 30,
                    },
                    "background": {"type": "boolean", "default": False},
                    "auto_background_on_timeout": {"type": "boolean", "default": True},
                    "env": {
                        "type": ["object", "null"],
                        "additionalProperties": {"type": "string"},
                        "default": None,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "process_manager",
            "title": "Manage background jobs and OS processes",
            "description": (
                "Inspect, poll output, send stdin, or kill background jobs and OS processes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_jobs",
                            "job_status",
                            "kill_job",
                            "send_input",
                            "clear_finished",
                            "system_processes",
                            "kill_pid",
                            "restart_bridge",
                        ],
                        "default": "list_jobs",
                    },
                    "job_id": {"type": ["string", "null"], "default": None},
                    "pid": {"type": ["integer", "null"], "default": None},
                    "input_text": {"type": ["string", "null"], "default": None},
                    "filter_name": {"type": ["string", "null"], "default": None},
                    "tail_chars": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 200000,
                        "default": 16000,
                    },
                },
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "system_info",
            "title": "System hardware and environment info",
            "description": (
                "Return OS details, CPU count, disk space on all drives, installed CLI tools, "
                "and active bridge configuration."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ]
    if enable_codex_tasks:
        tools.extend(_bridge_task_tool_definitions())
    names = visible_tool_names(enable_codex_tasks, tool_profile=tool_profile)
    tools = [tool for tool in tools if tool["name"] in names]
    for tool in tools:
        security_schemes = (
            [{"type": "noauth"}]
            if auth_mode == "connector_secret"
            else [{"type": "http", "scheme": "bearer"}]
        )
        tool["securitySchemes"] = security_schemes
        tool["_meta"] = {
            "securitySchemes": security_schemes,
            "visibility": "public",
            "toolInvocation/invoking": f"Running {tool['name']}",
            "toolInvocation/invoked": f"{tool['name']} finished",
        }
    return tools


def visible_tool_names(enable_codex_tasks: bool, *, tool_profile: ToolProfile = "full") -> set[str]:
    """Return the tool names visible to the AI agent for the selected profile."""

    if tool_profile == "delegate":
        names = {"bridge_status"}
        if enable_codex_tasks:
            names.update({"bridge_delegate", "bridge_session_list", "bridge_session_status"})
        return names
    names = {
        "bridge_status",
        "set_bridge_mode",
        "grant_path",
        "revoke_grant",
        "list_grants",
        "search_files",
        "read_file",
        "view_image",
        "write_file",
        "list_dir",
        "edit_file",
        "file_ops",
        "exec_command",
        "process_manager",
        "system_info",
    }
    if enable_codex_tasks:
        names.update(
            {
                "bridge_task_run",
                "bridge_delegate",
                "bridge_session_start",
                "bridge_session_continue",
                "bridge_session_list",
                "bridge_session_status",
            }
        )
    return names


def _bridge_task_tool_definitions() -> list[JsonObject]:
    delegate_permission_level = {
        "type": "string",
        "enum": [
            "read_only",
            "local_file_write",
            "repo_edit",
            "shell_command",
            "gui_control",
            "full_local",
        ],
        "default": "full_local",
        "description": (
            "The execution capability level. Defaults to full_local for direct, unhindered "
            "execution of local commands and project tasks."
        ),
    }
    return [
        {
            "name": "bridge_task_run",
            "title": "Run local task",
            "description": (
                "Execute one bounded task directly in an approved local directory."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "cwd": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "wait_timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 600,
                        "default": 120,
                    },
                },
                "required": ["prompt", "cwd"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "bridge_delegate",
            "title": "Delegate local task",
            "description": (
                "Delegate a complete local computer, file, terminal, build, test, GUI, or project "
                "task directly to the local execution engine. Provide a concrete task, optional paths, "
                "and expected result so the task executes directly to completion in a single operation."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20000,
                        "description": (
                            "The complete local task to perform. Include concrete "
                            "steps, file/app names, success criteria, and any user constraints."
                        ),
                    },
                    "prompt": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 20000,
                        "default": None,
                        "deprecated": True,
                        "description": (
                            "Deprecated compatibility alias for task. Prefer task for all new "
                            "calls."
                        ),
                    },
                    "permission_level": delegate_permission_level,
                    "target_paths": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                            "description": "Absolute or user-relative local path in scope.",
                        },
                        "maxItems": 32,
                        "default": [],
                        "description": (
                            "Local files or directories that may be read, written, edited, or "
                            "used as context. Leave empty for GUI-only or broad full_local tasks."
                        ),
                    },
                    "expected_result": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 4000,
                        "default": None,
                        "description": (
                            "What should be true when the task is done, for example a file exists, "
                            "tests pass, or a local app contains specific text."
                        ),
                    },
                    "cwd": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 1000,
                        "default": None,
                        "description": (
                            "Optional local working directory. Defaults to the user's "
                            "home directory."
                        ),
                    },
                    "bridge_session_id": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": (
                            "Optional latest or bridge_session_id to continue."
                        ),
                    },
                    "wait_timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1200,
                        "default": 600,
                    },
                },
                "required": ["permission_level"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "bridge_session_id": {"type": "string"},
                    "session_id": {"type": ["string", "null"]},
                    "cwd": {"type": "string"},
                    "returncode": {"type": "integer"},
                    "final_message": {"type": "string"},
                    "stdout_tail": {"type": "string"},
                    "stderr_tail": {"type": "string"},
                    "delegation_mode": {"type": "string", "enum": ["start", "continue"]},
                    "permission_level": delegate_permission_level,
                    "target_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "bridge_session_id",
                    "cwd",
                    "returncode",
                    "final_message",
                    "delegation_mode",
                    "permission_level",
                    "target_paths",
                ],
                "additionalProperties": True,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "bridge_session_start",
            "title": "Start local session",
            "description": (
                "Start a persistent local session for project changes, command execution, "
                "tests, or repo work."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "cwd": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "wait_timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1200,
                        "default": 600,
                    },
                },
                "required": ["prompt", "cwd"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "bridge_session_continue",
            "title": "Continue local session",
            "description": "Send a follow-up prompt to a persistent local session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "bridge_session_id": {
                        "type": "string",
                        "default": "latest",
                        "description": "Use latest or a bridge_session_id.",
                    },
                    "wait_timeout_s": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1200,
                        "default": 600,
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
        {
            "name": "bridge_session_list",
            "title": "List local sessions",
            "description": "List persistent local sessions known to the bridge.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "bridge_session_status",
            "title": "Get local session status",
            "description": "Return metadata for a persistent local session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "bridge_session_id": {
                        "type": "string",
                        "default": "latest",
                    },
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ]


def _auth_error(expected_token: str, authorization: str | None) -> JSONResponse | None:
    if authorization == f"Bearer {expected_token}":
        return None
    return JSONResponse(
        {"error": "missing or invalid bearer token"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _jsonrpc_error(request_id: int | str | None, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=200,
    )
