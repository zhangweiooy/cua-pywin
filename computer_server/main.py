import logging

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

OS_TYPE = "windows"

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

app = FastAPI(
    title="Computer API",
    description="API for the Computer project",
    version="0.1.0",
    lifespan=None,
    redirect_slashes=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id"],
)


@app.on_event("startup")
async def _mount_mcp():
    """Lazy-mount the MCP server on first HTTP startup."""
    if _check_mcp() and _mcp_server_app:
        app.mount("/mcp", _mcp_server_app)
        logger.info("MCP server mounted at /mcp")


@app.get("/status")
async def status():
    features = []
    if _check_mcp():
        features.append("mcp")
    return {
        "status": "ok",
        "os_type": OS_TYPE,
        "features": features,
        "mcp_endpoint": "/mcp",
        "http_api": "mcp_only",
        "note": "Legacy /ws and /commands APIs have been removed; use MCP at /mcp.",
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
