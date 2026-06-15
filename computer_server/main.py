import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .tool_rpc import (
    AUTH_HEADER,
    BATCH_HEADER,
    DAEMON_TOKEN_ENV,
    call_result_to_json,
    tool_to_json,
)

OS_TYPE = "windows"

HAS_MCP = None
_mcp_server = None
_mcp_server_app = None
_mcp_mounted = False
_tools_cache = None


def _get_mcp_server() -> Any | None:
    """Lazy-load MCP server for SSE integration.

    Kept lazy so ``python -m computer_server.mcp_server`` doesn't trigger a
    RuntimeWarning about the module already being in sys.modules, and so
    ``import computer_server`` doesn't pay the cost of building the MCP
    tool registry until the HTTP server is actually started.
    """
    global HAS_MCP, _mcp_server, _mcp_server_app
    if HAS_MCP is None:
        try:
            from .mcp_server import create_mcp_server as _create

            _mcp_server = _create()
            _mcp_server_app = _mcp_server.http_app(path="/")
            HAS_MCP = True
        except ImportError:
            _mcp_server = None
            HAS_MCP = False
    return _mcp_server


def _check_mcp() -> bool:
    return _get_mcp_server() is not None


async def _get_tools_cache() -> list[dict[str, Any]]:
    global _tools_cache
    if _tools_cache is None:
        mcp = _get_mcp_server()
        if mcp is None:
            raise HTTPException(status_code=503, detail="MCP server unavailable")
        _tools_cache = [tool_to_json(tool) for tool in await mcp.list_tools()]
    return _tools_cache


async def _require_cli_token(
    x_cua_token: str | None = Header(default=None, alias=AUTH_HEADER),
) -> None:
    token = os.environ.get(DAEMON_TOKEN_ENV)
    if not token:
        raise HTTPException(status_code=404, detail="CLI JSON RPC is not enabled")
    if x_cua_token != token:
        raise HTTPException(status_code=401, detail="missing or invalid daemon token")


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI):
    """Lazy-mount the MCP server on HTTP startup."""
    global _mcp_mounted
    if not _mcp_mounted and _check_mcp() and _mcp_server_app:
        fastapi_app.mount("/mcp", _mcp_server_app)
        _mcp_mounted = True
        logger.info("MCP server mounted at /mcp")
    yield


app = FastAPI(
    title="Computer API",
    description="API for the Computer project",
    version="0.1.2",
    lifespan=_lifespan,
    redirect_slashes=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Mcp-Session-Id",
        AUTH_HEADER,
        BATCH_HEADER,
    ],
)


@app.get("/status")
async def status():
    features = []
    if _check_mcp():
        features.append("mcp")
    result = {
        "status": "ok",
        "os_type": OS_TYPE,
        "features": features,
        "mcp_endpoint": "/mcp",
        "http_api": "mcp_only",
        "note": "Use MCP at /mcp.",
    }
    if os.environ.get(DAEMON_TOKEN_ENV):
        result.update(
            {
                "name": "cua-win",
                "pid": os.getpid(),
                "tool_count": len(await _get_tools_cache()),
                "cli_json_rpc": True,
                "http_api": "mcp_and_cli_json_rpc",
            }
        )
    return result


@app.get("/tools", dependencies=[Depends(_require_cli_token)])
async def list_tools() -> dict[str, Any]:
    tools = await _get_tools_cache()
    return {"tools": tools, "count": len(tools), "mode": "daemon"}


@app.get("/tools/{tool_name}", dependencies=[Depends(_require_cli_token)])
async def describe_tool(tool_name: str) -> dict[str, Any]:
    tools = await _get_tools_cache()
    for tool in tools:
        if tool.get("name") == tool_name:
            return {"tool": tool, "mode": "daemon"}
    raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")


@app.post("/call", dependencies=[Depends(_require_cli_token)])
async def call_tool(
    body: dict[str, Any] = Body(...),
    x_cua_batch: str | None = Header(default=None, alias=BATCH_HEADER),
) -> dict[str, Any]:
    name = body.get("name")
    arguments = body.get("arguments") or {}
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="arguments must be an object")

    mcp = _get_mcp_server()
    if mcp is None:
        raise HTTPException(status_code=503, detail="MCP server unavailable")
    batch_call = isinstance(x_cua_batch, str) and x_cua_batch.strip().lower() in {
        "1",
        "true",
    }
    if batch_call:
        from computer_server.overlay import suppress_animation

        with suppress_animation():
            result = await mcp.call_tool(name, arguments)
    else:
        result = await mcp.call_tool(name, arguments)
    payload = call_result_to_json(result)
    payload.update({"tool": name, "mode": "daemon"})
    return payload


@app.post("/shutdown", dependencies=[Depends(_require_cli_token)])
async def shutdown() -> dict[str, Any]:
    server = getattr(app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(status_code=500, detail="server state unavailable")

    async def stop_soon() -> None:
        await asyncio.sleep(0.1)
        server.should_exit = True

    asyncio.create_task(stop_soon())
    return {"success": True}
