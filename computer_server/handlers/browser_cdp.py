"""Chrome DevTools Protocol client for browser automation.

Connects to a browser's remote debugging port to execute JavaScript,
query DOM, and extract page content. Requires the browser to be launched
with --remote-debugging-port.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CDPClient:
    """Chrome DevTools Protocol client via HTTP + WebSocket.

    Connects to a browser's remote debugging port to execute JavaScript,
    query DOM, and extract page content.
    """

    def __init__(self) -> None:
        self._message_id = 0
        self._target_cache: Dict[tuple[str, int], tuple[float, List[Dict[str, Any]]]] = {}
        self._target_cache_ttl = 2.0

    def _next_id(self) -> int:
        """Get next JSON-RPC message ID."""
        self._message_id += 1
        return self._message_id

    async def discover(
        self, port: int = 9222, host: str = "localhost", force_refresh: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        """Discover available browser targets (tabs).

        GET http://{host}:{port}/json — list of targets

        Returns:
            List of target dicts [{id, title, url, type, webSocketDebuggerUrl}]
            or None if port unreachable.
        """
        import aiohttp

        cache_key = (host, int(port))
        if not force_refresh:
            cached = self._target_cache.get(cache_key)
            if cached is not None:
                cached_at, targets = cached
                if time.monotonic() - cached_at <= self._target_cache_ttl:
                    return targets

        url = f"http://{host}:{port}/json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        targets = await resp.json()
                        # Only return "page" type targets (exclude service workers, extensions)
                        pages = [t for t in targets if t.get("type") == "page"]
                        self._target_cache[cache_key] = (time.monotonic(), pages)
                        return pages
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

    async def _resolve_target(
        self,
        port: int,
        tab_index: int = 0,
        host: str = "localhost",
        target_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a page target by stable target id or initial tab index."""
        targets = await self.discover(port, host, force_refresh=force_refresh)
        if not targets:
            return None
        if target_id:
            for target in targets:
                if target.get("id") == target_id:
                    return target
            return None
        if tab_index >= len(targets):
            return None
        return targets[tab_index]

    async def _get_ws_url(
        self,
        port: int,
        tab_index: int = 0,
        host: str = "localhost",
        target_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Get WebSocket URL and metadata for a specific page target."""
        target = await self._resolve_target(
            port,
            tab_index=tab_index,
            host=host,
            target_id=target_id,
            force_refresh=force_refresh,
        )
        if not target:
            return None, None
        return target.get("webSocketDebuggerUrl"), target

    async def evaluate(
        self,
        port: int,
        expression: str,
        tab_index: int = 0,
        host: str = "localhost",
        target_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute JavaScript in the specified tab.

        Returns:
            {result: <JS return value>, error: Optional[str]}
        """
        ws_url, target = await self._get_ws_url(
            port,
            tab_index=tab_index,
            host=host,
            target_id=target_id,
        )
        if not ws_url:
            selector = f"target_id {target_id!r}" if target_id else f"tab index {tab_index}"
            return {
                "result": None,
                "error": f"No page target found at port {port} ({selector})",
            }
        locked_target_id = target.get("id") if target else target_id

        params = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        response = await self._send_command(ws_url, "Runtime.evaluate", params)
        if isinstance(response.get("error"), str):
            retry_ws_url, retry_target = await self._get_ws_url(
                port,
                tab_index=tab_index,
                host=host,
                target_id=locked_target_id,
                force_refresh=True,
            )
            if retry_ws_url and retry_ws_url != ws_url:
                if retry_target is not None:
                    locked_target_id = retry_target.get("id", locked_target_id)
                response = await self._send_command(
                    retry_ws_url, "Runtime.evaluate", params
                )

        if "error" in response and isinstance(response["error"], str):
            return {
                "result": None,
                "error": response["error"],
                "target_id": locked_target_id,
            }
        if "error" in response and isinstance(response["error"], dict):
            err_dict = response["error"]
            return {
                "result": None,
                "error": err_dict.get("message", str(err_dict)),
                "target_id": locked_target_id,
            }

        result_obj = response.get("result", {})
        if "exceptionDetails" in result_obj:
            exc = result_obj["exceptionDetails"]
            text = exc.get("text", "") or exc.get("exception", {}).get(
                "description", "Unknown error"
            )
            return {
                "result": None,
                "error": f"JavaScript error: {text}",
                "target_id": locked_target_id,
            }

        value = result_obj.get("result", {}).get("value")
        return {
            "result": value,
            "error": None,
            "target_id": locked_target_id,
            "target_url": target.get("url") if target else None,
            "target_title": target.get("title") if target else None,
        }

    async def get_page_text(
        self,
        port: int,
        tab_index: int = 0,
        host: str = "localhost",
        target_id: Optional[str] = None,
    ) -> str:
        """Get the text content of the page (document.body.innerText)."""
        result = await self.evaluate(
            port,
            "document.body.innerText",
            tab_index=tab_index,
            host=host,
            target_id=target_id,
        )
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
        allow_html: bool = False,
        target_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query DOM elements by CSS selector and extract attributes.

        Args:
            port: Chrome debugging port.
            selector: CSS selector.
            attributes: Attributes to extract (default: ["textContent", "href", "id", "class"]).
            tab_index: Initial tab index when target_id is not provided.
            host: Host address.
            target_id: Stable CDP target id. Prefer this after get_page_info.

        Returns:
            Dict with elements, error, and target_id.
        """
        if attributes is None:
            attributes = ["textContent", "href", "id", "class"]
        if not allow_html:
            attributes = [a for a in attributes if a != "innerHTML"]

        attrs_json = json.dumps(attributes)
        js = (
            "(function(){"
            f"const elements = document.querySelectorAll({json.dumps(selector)});"
            f"const attrs = {attrs_json};"
            "return Array.from(elements).slice(0, 100).map(el => {"
            "  const obj = {};"
            "  const tag = (el.tagName || '').toLowerCase();"
            "  const type = (el.getAttribute('type') || '').toLowerCase();"
            "  const sensitive = tag === 'input' && (type === 'password' || type === 'hidden');"
            "  attrs.forEach(attr => {"
            "    if (sensitive && (attr === 'value' || attr === 'textContent' || attr === 'innerHTML')) { return; }"
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

        result = await self.evaluate(
            port,
            js,
            tab_index=tab_index,
            host=host,
            target_id=target_id,
        )
        if result.get("error"):
            return {
                "elements": [],
                "error": result["error"],
                "target_id": result.get("target_id"),
            }
        return {
            "elements": result.get("result", []) or [],
            "error": None,
            "target_id": result.get("target_id"),
        }

    async def get_page_info(
        self,
        port: int,
        tab_index: int = 0,
        host: str = "localhost",
        target_id: Optional[str] = None,
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
        result = await self.evaluate(
            port,
            js,
            tab_index=tab_index,
            host=host,
            target_id=target_id,
        )
        if result.get("error"):
            return {
                "title": "",
                "url": "",
                "domain": "",
                "target_id": result.get("target_id"),
                "error": result["error"],
            }
        info = result.get("result", {}) or {}
        info["target_id"] = result.get("target_id")
        return info


# Module-level singleton
_cdp_client: Optional[CDPClient] = None


def get_cdp_client() -> CDPClient:
    """Get the global CDPClient singleton."""
    global _cdp_client
    if _cdp_client is None:
        _cdp_client = CDPClient()
    return _cdp_client
