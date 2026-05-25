"""Chrome DevTools Protocol client for browser automation.

Connects to a browser's remote debugging port to execute JavaScript,
query DOM, and extract page content. Requires the browser to be launched
with --remote-debugging-port.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CDPClient:
    """Chrome DevTools Protocol client via HTTP + WebSocket.

    Connects to a browser's remote debugging port to execute JavaScript,
    query DOM, and extract page content.
    """

    def __init__(self) -> None:
        self._message_id = 0

    def _next_id(self) -> int:
        """Get next JSON-RPC message ID."""
        self._message_id += 1
        return self._message_id

    async def discover(
        self, port: int = 9222, host: str = "localhost"
    ) -> Optional[List[Dict[str, Any]]]:
        """Discover available browser targets (tabs).

        GET http://{host}:{port}/json — list of targets

        Returns:
            List of target dicts [{id, title, url, type, webSocketDebuggerUrl}]
            or None if port unreachable.
        """
        import aiohttp

        url = f"http://{host}:{port}/json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        targets = await resp.json()
                        # Only return "page" type targets (exclude service workers, extensions)
                        return [t for t in targets if t.get("type") == "page"]
                    else:
                        logger.warning("CDP discover failed: HTTP %s", resp.status)
                        return None
        except Exception as e:
            logger.warning("CDP discover failed: %s", e)
            return None

    async def _send_command(
        self,
        ws_url: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Send a CDP command via WebSocket and wait for response."""
        try:
            import websockets
        except ImportError:
            return {"error": "websockets package not installed. Install with: pip install websockets"}

        msg_id = self._next_id()
        message: Dict[str, Any] = {
            "id": msg_id,
            "method": method,
            "params": params or {},
        }

        try:
            async with websockets.connect(
                ws_url, close_timeout=2, open_timeout=timeout
            ) as ws:
                await ws.send(json.dumps(message))

                # Wait for response with matching ID
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return {"error": f"Timeout waiting for CDP response (method={method})"}

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        response = json.loads(raw)
                        if response.get("id") == msg_id:
                            return response
                        # Ignore events and other messages
                    except asyncio.TimeoutError:
                        return {"error": f"Timeout waiting for CDP response (method={method})"}
        except Exception as e:
            return {"error": f"CDP WebSocket error: {str(e)}"}

    async def _get_ws_url(
        self, port: int, tab_index: int = 0, host: str = "localhost"
    ) -> Optional[str]:
        """Get WebSocket URL for a specific tab."""
        targets = await self.discover(port, host)
        if not targets:
            return None
        if tab_index >= len(targets):
            return None
        return targets[tab_index].get("webSocketDebuggerUrl")

    async def evaluate(
        self,
        port: int,
        expression: str,
        tab_index: int = 0,
        host: str = "localhost",
    ) -> Dict[str, Any]:
        """Execute JavaScript in the specified tab.

        Returns:
            {result: <JS return value>, error: Optional[str]}
        """
        ws_url = await self._get_ws_url(port, tab_index, host)
        if not ws_url:
            return {
                "result": None,
                "error": f"No page target found at port {port} (tab index {tab_index})",
            }

        response = await self._send_command(
            ws_url,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )

        if "error" in response and isinstance(response["error"], str):
            return {"result": None, "error": response["error"]}
        if "error" in response and isinstance(response["error"], dict):
            err_dict = response["error"]
            return {"result": None, "error": err_dict.get("message", str(err_dict))}

        result_obj = response.get("result", {})
        if "exceptionDetails" in result_obj:
            exc = result_obj["exceptionDetails"]
            text = exc.get("text", "") or exc.get("exception", {}).get(
                "description", "Unknown error"
            )
            return {"result": None, "error": f"JavaScript error: {text}"}

        value = result_obj.get("result", {}).get("value")
        return {"result": value, "error": None}

    async def get_page_text(
        self, port: int, tab_index: int = 0, host: str = "localhost"
    ) -> str:
        """Get the text content of the page (document.body.innerText)."""
        result = await self.evaluate(port, "document.body.innerText", tab_index, host)
        if result.get("error"):
            return f"Error: {result['error']}"
        return result.get("result", "") or ""

    async def query_dom(
        self,
        port: int,
        selector: str,
        attributes: Optional[List[str]] = None,
        tab_index: int = 0,
        host: str = "localhost",
    ) -> List[Dict[str, Any]]:
        """Query DOM elements by CSS selector and extract attributes.

        Args:
            port: Chrome debugging port.
            selector: CSS selector.
            attributes: Attributes to extract (default: ["textContent", "href", "id", "class"]).
            tab_index: Tab index.
            host: Host address.

        Returns:
            List of element dicts with requested attributes.
        """
        if attributes is None:
            attributes = ["textContent", "href", "id", "class"]

        attrs_json = json.dumps(attributes)
        js = (
            "(function(){"
            f"const elements = document.querySelectorAll({json.dumps(selector)});"
            f"const attrs = {attrs_json};"
            "return Array.from(elements).slice(0, 100).map(el => {"
            "  const obj = {};"
            "  attrs.forEach(attr => {"
            "    if (attr === 'textContent') { obj[attr] = (el.textContent || '').trim().substring(0, 500); }"
            "    else if (attr === 'innerHTML') { obj[attr] = (el.innerHTML || '').substring(0, 1000); }"
            "    else {"
            "      const v = el.getAttribute(attr);"
            "      if (v !== null) obj[attr] = v;"
            "    }"
            "  });"
            "  return obj;"
            "});"
            "})()"
        )

        result = await self.evaluate(port, js, tab_index, host)
        if result.get("error"):
            return []
        return result.get("result", []) or []

    async def get_page_info(
        self, port: int, tab_index: int = 0, host: str = "localhost"
    ) -> Dict[str, Any]:
        """Get basic page information (title, URL, domain)."""
        js = (
            "(function(){"
            "  return {"
            "    title: document.title,"
            "    url: window.location.href,"
            "    domain: window.location.hostname"
            "  };"
            "})()"
        )
        result = await self.evaluate(port, js, tab_index, host)
        if result.get("error"):
            return {"title": "", "url": "", "domain": "", "error": result["error"]}
        return result.get("result", {}) or {}


# Module-level singleton
_cdp_client: Optional[CDPClient] = None


def get_cdp_client() -> CDPClient:
    """Get the global CDPClient singleton."""
    global _cdp_client
    if _cdp_client is None:
        _cdp_client = CDPClient()
    return _cdp_client
