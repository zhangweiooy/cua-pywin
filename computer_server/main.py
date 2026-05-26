import asyncio
import inspect
import json
import logging
import traceback
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .handlers.factory import OS_TYPE, HandlerFactory

HAS_MCP = None
_mcp_server_app = None


def _check_mcp() -> bool:
    """Lazy-load MCP server for SSE integration.

    Kept lazy so ``python -m computer_server.mcp_server`` doesn't trigger a
    RuntimeWarning about the module already being in sys.modules, and so
    ``import computer_server`` doesn't pay the cost of building the MCP
    tool registry until the HTTP server is actually started.
    """
    global HAS_MCP, _mcp_server_app
    if HAS_MCP is None:
        try:
            from .mcp_server import create_mcp_server as _create

            _mcp = _create()
            _mcp_server_app = _mcp.http_app(path="/")
            HAS_MCP = True
        except ImportError:
            HAS_MCP = False
    return HAS_MCP


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

WEBSOCKET_MAX_SIZE = 1024 * 1024 * 10

app = FastAPI(
    title="Computer API",
    description="API for the Computer project",
    version="0.1.0",
    websocket_max_size=WEBSOCKET_MAX_SIZE,
    lifespan=None,
    redirect_slashes=False,
)

# CORS
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _mount_mcp():
    """Lazy-mount the MCP server on first HTTP startup."""
    if _check_mcp() and _mcp_server_app:
        app.mount("/mcp", _mcp_server_app)
        logger.info("MCP server mounted at /mcp")


protocol_version = 1
try:
    from importlib.metadata import version

    package_version = version("cua-pywin")
except Exception:
    package_version = "unknown"

_handlers = HandlerFactory.create_handlers()
accessibility_handler = _handlers.accessibility
automation_handler = _handlers.automation
file_handler = _handlers.file
desktop_handler = _handlers.desktop
window_handler = _handlers.window


async def _scroll_direction_handler(direction: str, clicks: int = 1) -> Dict[str, Any]:
    direction = direction.lower()
    if direction == "down":
        return await automation_handler.scroll_down(clicks)
    elif direction == "up":
        return await automation_handler.scroll_up(clicks)
    elif direction in ("left", "right"):
        x_amount = -300 if direction == "left" else 300
        return await automation_handler.scroll(x_amount * clicks, 0)
    else:
        raise ValueError(f"Invalid direction: {direction}. Use 'up', 'down', 'left', or 'right'.")


COMMAND_ALIASES = {
    "type": "type_text",
    "key": "press_key",
    "tap": "left_click",
    "click": "left_click",
    "shell": "run_command",
    "exec": "run_command",
    "read_file": "read_text",
    "write_file": "write_text",
    "ls": "list_dir",
    "mkdir": "create_dir",
    "rm": "delete_file",
    "rmdir": "delete_dir",
}

handlers = {
    "version": lambda: {"protocol": protocol_version, "package": package_version},
"get_accessibility_tree": accessibility_handler.get_accessibility_tree,
    "run_command": automation_handler.run_command,
    "file_exists": file_handler.file_exists,
    "directory_exists": file_handler.directory_exists,
    "list_dir": file_handler.list_dir,
    "read_text": file_handler.read_text,
    "write_text": file_handler.write_text,
    "read_bytes": file_handler.read_bytes,
    "write_bytes": file_handler.write_bytes,
    "get_file_size": file_handler.get_file_size,
    "delete_file": file_handler.delete_file,
    "create_dir": file_handler.create_dir,
    "delete_dir": file_handler.delete_dir,
    "get_desktop_environment": desktop_handler.get_desktop_environment,
    "set_wallpaper": desktop_handler.set_wallpaper,
    "open": window_handler.open,
    "launch": window_handler.launch,
    "get_current_window_id": window_handler.get_current_window_id,
    "get_application_windows": window_handler.get_application_windows,
    "get_window_name": window_handler.get_window_name,
    "get_window_size": window_handler.get_window_size,
    "get_window_position": window_handler.get_window_position,
    "set_window_size": window_handler.set_window_size,
    "set_window_position": window_handler.set_window_position,
    "maximize_window": window_handler.maximize_window,
    "minimize_window": window_handler.minimize_window,
    "activate_window": window_handler.activate_window,
    "close_window": window_handler.close_window,
    "mouse_down": automation_handler.mouse_down,
    "mouse_up": automation_handler.mouse_up,
    "left_click": automation_handler.left_click,
    "right_click": automation_handler.right_click,
    "double_click": automation_handler.double_click,
    "move_cursor": automation_handler.move_cursor,
    "drag_to": automation_handler.drag_to,
    "drag": automation_handler.drag,
    "key_down": automation_handler.key_down,
    "key_up": automation_handler.key_up,
    "type_text": automation_handler.type_text,
    "press_key": automation_handler.press_key,
    "hotkey": automation_handler.hotkey,
    "scroll": automation_handler.scroll,
    "scroll_down": automation_handler.scroll_down,
    "scroll_up": automation_handler.scroll_up,
    "scroll_direction": _scroll_direction_handler,
    "screenshot": automation_handler.screenshot,
    "get_cursor_position": automation_handler.get_cursor_position,
    "get_screen_size": automation_handler.get_screen_size,
    "copy_to_clipboard": automation_handler.copy_to_clipboard,
    "set_clipboard": automation_handler.set_clipboard,
}

def _resolve_command(command: str) -> str:
    return COMMAND_ALIASES.get(command, command)


def _suggest_similar_commands(command: str) -> List[str]:
    all_commands = list(handlers.keys()) + list(COMMAND_ALIASES.keys())
    suggestions = []
    command_lower = command.lower()
    for cmd in all_commands:
        if cmd.lower().startswith(command_lower[:2]) or command_lower in cmd.lower():
            suggestions.append(cmd)
    return suggestions[:5]


@app.get("/status")
async def status():
    features = []
    if _check_mcp():
        features.append("mcp")
    return {
        "status": "ok",
        "os_type": OS_TYPE,
        "features": features,
        "http_api": "legacy",
        "mcp_endpoint": "/mcp",
        "note": "The /ws command API is a legacy subset. Prefer MCP for full tool coverage.",
    }


@app.get("/commands")
async def list_commands():
    """List all available commands and their aliases."""
    commands_info = {}
    for cmd_name, handler_func in handlers.items():
        try:
            sig = inspect.signature(handler_func)
            params = [
                {
                    "name": p.name,
                    "required": p.default == inspect.Parameter.empty,
                    "default": None if p.default == inspect.Parameter.empty else p.default,
                }
                for p in sig.parameters.values()
            ]
        except (ValueError, TypeError):
            params = []
        commands_info[cmd_name] = {"params": params}

    aliases_by_command: Dict[str, List[str]] = {}
    for alias, canonical in COMMAND_ALIASES.items():
        if canonical not in aliases_by_command:
            aliases_by_command[canonical] = []
        aliases_by_command[canonical].append(alias)

    for cmd_name in commands_info:
        if cmd_name in aliases_by_command:
            commands_info[cmd_name]["aliases"] = aliases_by_command[cmd_name]

    return {
        "api": "legacy_http_subset",
        "mcp_endpoint": "/mcp",
        "commands": commands_info,
        "aliases": COMMAND_ALIASES,
    }


@app.websocket("/ws", name="websocket_endpoint")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            try:
                data = await websocket.receive_json()
                command = data.get("command")
                params = data.get("params", {})

                command = _resolve_command(command)

                if command not in handlers:
                    suggestions = _suggest_similar_commands(command)
                    error_msg = f"Unknown command: {command}"
                    if suggestions:
                        error_msg += f". Did you mean: {', '.join(suggestions)}?"
                    await websocket.send_json({"success": False, "error": error_msg})
                    continue

                try:
                    handler_func = handlers[command]
                    sig = inspect.signature(handler_func)
                    filtered_params = {k: v for k, v in params.items() if k in sig.parameters}

                    if asyncio.iscoroutinefunction(handler_func):
                        result = await handler_func(**filtered_params)
                    else:
                        result = await asyncio.to_thread(handler_func, **filtered_params)
                    await websocket.send_json({"success": True, **result})
                except Exception as cmd_error:
                    logger.error(f"Error executing command {command}: {str(cmd_error)}")
                    logger.error(traceback.format_exc())
                    await websocket.send_json({"success": False, "error": str(cmd_error)})

            except WebSocketDisconnect:
                raise
            except json.JSONDecodeError as json_err:
                logger.error(f"JSON decode error: {str(json_err)}")
                await websocket.send_json(
                    {"success": False, "error": f"Invalid JSON: {str(json_err)}"}
                )
            except Exception as loop_error:
                logger.error(f"Error in message loop: {str(loop_error)}")
                logger.error(traceback.format_exc())
                await websocket.send_json({"success": False, "error": str(loop_error)})

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"Fatal error in websocket connection: {str(e)}")
        logger.error(traceback.format_exc())
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
