import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
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


def _transport_error_payload(
    message: str,
    *,
    code: str = "INVALID_REQUEST",
    retry_safety: str = "safe",
) -> dict[str, Any]:
    return {
        "protocol_version": "0.2",
        "result": None,
        "content": [],
        "transport_error": {
            "code": code,
            "message": message,
            "retry_safety": retry_safety,
        },
    }


async def _call_token_error(
    x_cua_token: str | None = Header(default=None, alias=AUTH_HEADER),
) -> dict[str, Any] | None:
    """Return a protocol transport error instead of raising for /call."""
    token = os.environ.get(DAEMON_TOKEN_ENV)
    if not token:
        return _transport_error_payload(
            "CLI JSON RPC is not enabled",
            code="TRANSPORT_ERROR",
            retry_safety="do_not_retry",
        )
    if x_cua_token != token:
        return _transport_error_payload(
            "missing or invalid daemon token",
            code="PERMISSION_DENIED",
            retry_safety="do_not_retry",
        )
    return None


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
    version="0.4.0",
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


@app.exception_handler(RequestValidationError)
async def _request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if request.url.path == "/call":
        return JSONResponse(
            status_code=200,
            content=_transport_error_payload(
                "request body is not valid JSON",
            ),
        )
    return await request_validation_exception_handler(request, exc)


@app.get("/status")
async def status():
    features = []
    if _check_mcp():
        features.append("mcp")
    result = {
        "status": "ok",
        "version": __version__,
        "protocol_version": "0.2",
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


@app.post("/call")
async def call_tool(
    body: Any = Body(default=None),
    x_cua_batch: str | None = Header(default=None, alias=BATCH_HEADER),
    token_error: dict[str, Any] | None = Depends(_call_token_error),
) -> dict[str, Any]:
    if token_error is not None:
        return token_error
    if not isinstance(body, dict):
        return _transport_error_payload("request body must be an object")
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return _transport_error_payload("name is required")
    raw_arguments = body.get("arguments")
    arguments = {} if raw_arguments is None else raw_arguments
    if not isinstance(arguments, dict):
        return _transport_error_payload("arguments must be an object")

    batch_call = isinstance(x_cua_batch, str) and x_cua_batch.strip().lower() in {
        "1",
        "true",
    }
    try:
        mcp = _get_mcp_server()
        if mcp is None:
            return _transport_error_payload(
                "MCP server unavailable",
                code="TRANSPORT_ERROR",
                retry_safety="reobserve",
            )
        if batch_call:
            from computer_server.overlay import suppress_animation

            with suppress_animation():
                result = await mcp.call_tool(name, arguments)
        else:
            result = await mcp.call_tool(name, arguments)
        return call_result_to_json(result)
    except Exception as exc:
        return _transport_error_payload(
            str(exc),
            code="TRANSPORT_ERROR",
            retry_safety="reobserve",
        )


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
