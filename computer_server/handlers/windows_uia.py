"""Windows UI Automation (UIA) deep tree traversal module.

Produces a flat list of nodes (with actionable nodes assigned an element index)
and a markdown-formatted tree representation.

This module is Windows-only. On non-Windows platforms it exposes empty stubs so
that the surrounding code can import it without crashing.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from computer_server.handlers.control_types import CONTROL_TYPE_NAMES, uia_control_type_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses (cross-platform)
# ---------------------------------------------------------------------------


@dataclass
class UiaNode:
    """A single node captured from the UIA traversal."""

    element_index: Optional[int]
    control_type: str
    name: Optional[str]
    automation_id: Optional[str]
    value: Optional[str] = None
    class_name: Optional[str] = None
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)  # (left, top, right, bottom)
    center_x: int = 0
    center_y: int = 0
    actions: List[str] = field(default_factory=list)
    element_ref: Any = None  # Deprecated: COM refs are not exported across calls
    depth: int = 0
    is_enabled: bool = True
    is_offscreen: bool = False


@dataclass
class UiaTreeResult:
    """Result of a walk_uia_tree call."""

    tree_markdown: str
    nodes: List[UiaNode] = field(default_factory=list)
    indexed_nodes: List[UiaNode] = field(default_factory=list)
    tree_quality: str = "empty"
    total_elements: int = 0


IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Cross-platform helpers
# ---------------------------------------------------------------------------


def filter_tree(tree_markdown: str, query: Optional[str]) -> str:
    """Filter a markdown tree, keeping matching lines and their ancestors.

    Matching is case-insensitive substring matching. Each kept line drags
    along its ancestor chain (lines with strictly smaller leading-space
    indentation that appear earlier in the document).
    """
    if not tree_markdown:
        return ""
    if not query:
        return tree_markdown

    needle = query.lower()
    lines = tree_markdown.splitlines()
    if not lines:
        return ""

    indents: List[int] = []
    for line in lines:
        stripped = line.lstrip(" ")
        indents.append(len(line) - len(stripped))

    keep = [False] * len(lines)
    for i, line in enumerate(lines):
        if needle not in line.lower():
            continue
        keep[i] = True
        target_indent = indents[i]
        j = i - 1
        while j >= 0:
            if keep[j]:
                j -= 1
                continue
            if indents[j] < target_indent:
                keep[j] = True
                target_indent = indents[j]
            j -= 1

    kept = [line for idx, line in enumerate(lines) if keep[idx]]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Non-Windows stub implementation
# ---------------------------------------------------------------------------

if not IS_WINDOWS:

    def walk_uia_tree(
        hwnd: int,
        query: Optional[str] = None,
        max_depth: int = 25,
        max_elements: int = 5000,
    ) -> UiaTreeResult:
        """Stub for non-Windows platforms. Always returns an empty result."""
        return UiaTreeResult(
            tree_markdown="",
            indexed_nodes=[],
            tree_quality="empty",
            total_elements=0,
        )

    def get_uia_instance() -> Any:
        """Stub that raises on non-Windows platforms."""
        raise RuntimeError("UIA is only available on Windows")

else:
    # -----------------------------------------------------------------------
    # Windows implementation
    # -----------------------------------------------------------------------

    import ctypes
    import comtypes
    from comtypes import COMError

    # Note on COM call timeouts:
    #
    # An earlier implementation submitted every UIA property/pattern call
    # to a shared ThreadPoolExecutor and used `Future.result(timeout=...)`
    # as a soft-cancel mechanism.  That design was flawed in two ways:
    #
    # 1. UIA COM objects are STA-affine.  Calling them from a *different*
    #    thread than the one that materialised them cross-marshals every
    #    call back to the originating apartment, defeating any thread-pool
    #    "isolation" the timeout was supposed to provide.
    # 2. When a target window is hung, the underlying COM call never
    #    returns — `Future.result(timeout=...)` only releases the *waiter*;
    #    the worker thread stays blocked forever.  Across many calls, the
    #    pool is permanently drained.
    #
    # The fix: call COM directly inside the STA thread that owns the UIA
    # instance (the executor thread that runs walk_uia_tree).  Per-call
    # timeout is impossible without killing the thread, which Python does
    # not support safely.  Instead, hangs are bounded one level up: the
    # caller (mcp_server.computer_get_window_state) wraps the entire walk
    # in `asyncio.wait_for` and uses an in-flight gate + unhealthy cooldown
    # so a stuck window cannot keep enqueuing fresh executor jobs.
    _UIA_COM_TIMEOUT = 3.0  # retained for API compatibility; unused

    def _com_call(fn, timeout=_UIA_COM_TIMEOUT, default=None):
        """Direct call into UIA — no cross-thread submission.

        This used to submit *fn* to a shared executor and wait with a
        timeout.  See the note above for why that approach leaked
        threads.  We now call directly and let the outer walk-level
        gate bound the worst case.
        """
        try:
            return fn()
        except (COMError, AttributeError, ValueError, TypeError):
            return default
        except Exception:
            return default

    # Per-thread COM init is done inside walk_uia_tree().

    # Make sure BoundingRectangle returns physical pixels regardless of the
    # current process DPI virtualization mode.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:
        logger.debug("SetProcessDPIAware failed (ignored): %s", exc)

    # Generate / Load the UIAutomationCore typelib bindings on first import.
    # comtypes 1.4.x lazy-loads submodules — explicit import required.
    try:
        import comtypes.client  # noqa: F811
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA  # type: ignore
    except Exception as exc:
        logger.error("Failed to load UIAutomationCore.dll: %s", exc)
        UIA = None  # type: ignore[assignment]

    # Pattern IDs
    UIA_InvokePatternId = 10000
    UIA_ValuePatternId = 10002
    UIA_ExpandCollapsePatternId = 10005
    UIA_SelectionItemPatternId = 10010
    UIA_TogglePatternId = 10015
    UIA_TextPatternId = 10014

    # Order matters — it determines the order of action names per node.
    PATTERN_CHECKS: List[Tuple[int, str]] = [
        (UIA_InvokePatternId, "invoke"),
        (UIA_TogglePatternId, "toggle"),
        (UIA_ExpandCollapsePatternId, "expand"),
        (UIA_SelectionItemPatternId, "select"),
        (UIA_TextPatternId, "text"),
        (UIA_ValuePatternId, "set_value"),
    ]

    # Property IDs
    UIA_NamePropertyId = 30005
    UIA_ControlTypePropertyId = 30003
    UIA_ValueValuePropertyId = 30045
    UIA_AutomationIdPropertyId = 30011
    UIA_ClassNamePropertyId = 30012
    UIA_BoundingRectanglePropertyId = 30001
    UIA_IsEnabledPropertyId = 30010
    UIA_IsOffscreenPropertyId = 30022

    # Control-type ID → human readable name (UIA_*ControlTypeId values)
    _CONTROL_TYPE_NAMES: Dict[int, str] = CONTROL_TYPE_NAMES

    # Control-type names that are structural layout containers.  When such an
    # element has no name/value text content it is treated as "zero depth
    # cost" during tree walking — children inherit the same depth budget.
    # This lets a depth=25 walk reach content nested 30-50 DOM levels deep
    # inside modern web and Electron apps.
    _LAYOUT_ONLY_NAMES: Set[str] = {
        "List",
        "Menu",
        "MenuBar",
        "StatusBar",
        "Tab",
        "ToolBar",
        "Tree",
        "Group",
        "DataGrid",
        "Window",
        "Pane",
        "Header",
        "Table",
        "TitleBar",
        "SemanticZoom",
        "AppBar",
    }

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _control_type_name(ctype_id: Any) -> str:
        return uia_control_type_name(ctype_id)

    def _get_prop(element: Any, prop_id: int, default: Any = None) -> Any:
        """Read a property — tries cached first, then current.

        Each COM call is guarded by _com_call() with a per-element timeout
        so a hung target window doesn't deadlock the entire tree walk.
        """
        try:
            value = _com_call(lambda: element.GetCachedPropertyValue(prop_id))
            if value is not None:
                return value
        except (COMError, AttributeError, ValueError, TypeError):
            pass
        try:
            return _com_call(lambda: element.GetCurrentPropertyValue(prop_id), default=default)
        except (COMError, AttributeError, ValueError, TypeError):
            return default

    def _str_or_none(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            text = str(value)
        except Exception:
            return None
        if not text:
            return None
        return text

    def _bbox_from_rect(rect: Any) -> Tuple[int, int, int, int]:
        """UIA returns (left, top, width, height) as a 4-element array."""
        try:
            if rect is None:
                return (0, 0, 0, 0)
            values = list(rect)
            if len(values) < 4:
                return (0, 0, 0, 0)
            left = int(values[0])
            top = int(values[1])
            width = int(values[2])
            height = int(values[3])
            return (left, top, left + width, top + height)
        except Exception:
            return (0, 0, 0, 0)

    def _detect_actions(element: Any) -> List[str]:
        """Return action names supported by the element via UIA patterns.

        Tries GetCachedPattern first (in-process, near-zero cost when the
        cache request included AddPattern).  Falls back to GetCurrentPattern
        only on cache miss — important for WebView2 / Electron providers
        that don't fully support pattern caching.
        """
        actions: List[str] = []
        for pattern_id, action_name in PATTERN_CHECKS:
            try:
                pattern = element.GetCachedPattern(pattern_id)
                if pattern is not None:
                    actions.append(action_name)
                    continue
            except (COMError, AttributeError, ValueError, TypeError):
                pass
            pattern = _com_call(lambda pid=pattern_id: element.GetCurrentPattern(pid))
            if pattern is not None:
                actions.append(action_name)
        return actions

    # -------------------------------------------------------------------
    # UIA + cache request setup
    # -------------------------------------------------------------------

    _thread_local = threading.local()

    def get_uia_instance() -> Any:
        """Get or create a thread-local IUIAutomation instance."""
        if not hasattr(_thread_local, "uia"):
            if UIA is None:
                raise RuntimeError("UIAutomationCore typelib unavailable")
            _thread_local.uia = comtypes.client.CreateObject(
                UIA.CUIAutomation, interface=UIA.IUIAutomation
            )
        return _thread_local.uia

    def _co_initialize_sta() -> bool:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            return True
        except OSError:
            return False

    def _co_uninitialize_if_needed(initialized: bool) -> None:
        if not initialized:
            return
        try:
            if hasattr(_thread_local, "uia"):
                delattr(_thread_local, "uia")
        except Exception:
            pass
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass

    def _uia_element_from_point(uia: Any, x: int, y: int) -> Any:
        """Resolve the current UIA element at a physical screen point."""
        try:
            from ctypes import wintypes

            return uia.ElementFromPoint(wintypes.POINT(int(x), int(y)))
        except Exception:
            return uia.ElementFromPoint((int(x), int(y)))

    def invoke_at_point(x: int, y: int) -> None:
        """Resolve a fresh UIA element at a point and invoke it."""
        initialized = _co_initialize_sta()
        try:
            uia = get_uia_instance()
            element = _uia_element_from_point(uia, x, y)
            pattern = element.GetCurrentPattern(UIA_InvokePatternId)
            pattern.Invoke()
        finally:
            _co_uninitialize_if_needed(initialized)

    def set_value_at_point(x: int, y: int, value: str) -> None:
        """Resolve a fresh UIA element at a point and set ValuePattern."""
        initialized = _co_initialize_sta()
        try:
            uia = get_uia_instance()
            element = _uia_element_from_point(uia, x, y)
            pattern = element.GetCurrentPattern(UIA_ValuePatternId)
            pattern.SetValue(value)
        finally:
            _co_uninitialize_if_needed(initialized)

    def _build_cache_request(uia: Any) -> Any:
        cache_request = uia.CreateCacheRequest()
        for prop_id in (
            UIA_NamePropertyId,
            UIA_ControlTypePropertyId,
            UIA_ValueValuePropertyId,
            UIA_AutomationIdPropertyId,
            UIA_ClassNamePropertyId,
            UIA_BoundingRectanglePropertyId,
            UIA_IsEnabledPropertyId,
            UIA_IsOffscreenPropertyId,
        ):
            try:
                cache_request.AddProperty(prop_id)
            except COMError as exc:
                logger.debug("AddProperty %s failed: %s", prop_id, exc)
        for pat_id, _pat_name in PATTERN_CHECKS:
            try:
                cache_request.AddPattern(pat_id)
            except COMError as exc:
                logger.debug("AddPattern %s failed: %s", pat_id, exc)
        return cache_request

    # -------------------------------------------------------------------
    # Internal traversal state
    # -------------------------------------------------------------------

    @dataclass
    class _WalkState:
        nodes: List[UiaNode] = field(default_factory=list)
        indexed_nodes: List[UiaNode] = field(default_factory=list)
        markdown_lines: List[str] = field(default_factory=list)
        next_index: int = 0
        total_visited: int = 0
        named_count: int = 0
        max_depth: int = 25
        max_elements: int = 5000
        stopped: bool = False

    def _format_markdown_line(node: UiaNode) -> str:
        indent = "  " * node.depth
        parts: List[str] = []

        if node.element_index is not None:
            parts.append(f"[{node.element_index}]")
        parts.append(node.control_type)

        if node.name:
            safe = node.name.replace('"', '\\"')
            parts.append(f'"{safe}"')

        if node.value:
            safe_val = node.value.replace('"', '\\"')
            parts.append(f'="{safe_val}"')

        meta_pieces: List[str] = []
        if node.automation_id:
            meta_pieces.append(f"id={node.automation_id}")
        if node.actions:
            meta_pieces.append(f"actions=[{','.join(node.actions)}]")

        if meta_pieces:
            parts.append("[" + ", ".join(meta_pieces) + "]")

        return f"{indent}{' '.join(parts)}"

    # -------------------------------------------------------------------
    # Element extraction
    # -------------------------------------------------------------------

    def _extract_node(element: Any, depth: int) -> Optional[UiaNode]:
        """Read properties from an element. Returns None on hard COM failure."""
        try:
            ctype_id = _get_prop(element, UIA_ControlTypePropertyId, 0)
            name = _str_or_none(_get_prop(element, UIA_NamePropertyId))
            value = _str_or_none(_get_prop(element, UIA_ValueValuePropertyId))
            automation_id = _str_or_none(_get_prop(element, UIA_AutomationIdPropertyId))
            class_name = _str_or_none(_get_prop(element, UIA_ClassNamePropertyId))
            rect = _get_prop(element, UIA_BoundingRectanglePropertyId)
            bbox = _bbox_from_rect(rect)
            is_enabled = bool(_get_prop(element, UIA_IsEnabledPropertyId, False))
            is_offscreen = bool(_get_prop(element, UIA_IsOffscreenPropertyId, False))
            actions = _detect_actions(element)
        except COMError as exc:
            logger.debug("COMError extracting node at depth %d: %s", depth, exc)
            return None
        except Exception as exc:
            logger.debug("Error extracting node at depth %d: %s", depth, exc)
            return None

        center_x = (bbox[0] + bbox[2]) // 2
        center_y = (bbox[1] + bbox[3]) // 2

        return UiaNode(
            element_index=None,
            control_type=_control_type_name(ctype_id),
            name=name,
            value=value,
            automation_id=automation_id,
            class_name=class_name,
            actions=actions,
            bbox=bbox,
            center_x=center_x,
            center_y=center_y,
            element_ref=None,
            depth=depth,
            is_enabled=is_enabled,
            is_offscreen=is_offscreen,
        )

    # -------------------------------------------------------------------
    # Node classification
    # -------------------------------------------------------------------

    def _is_actionable(node: UiaNode) -> bool:
        if not node.actions:
            return False
        if not node.is_enabled:
            return False
        if node.is_offscreen:
            return False
        if node.bbox == (0, 0, 0, 0):
            return False
        return True

    def _has_content(node: UiaNode) -> bool:
        return bool(node.name) or bool(node.value)

    def _is_layout_only(node: UiaNode) -> bool:
        """True when the node is a structural container that carries no semantics.

        Layout-only types (Group, Pane, ToolBar, etc.) are pure DOM/UI wrappers.
        When they have no text content, children inherit the same depth budget
        during tree walking (semantic depth tunneling).
        """
        if node.control_type in _LAYOUT_ONLY_NAMES:
            return True
        # Some apps use "Custom" for WPF Border/Grid containers.
        # Treat Custom as layout-only when it has no identity at all.
        if node.control_type == "Custom" and not node.name and not node.value and not node.automation_id:
            return True
        return False

    # -------------------------------------------------------------------
    # Recorder
    # -------------------------------------------------------------------

    def _record_node(node: UiaNode, state: _WalkState) -> None:
        if node.name or node.automation_id:
            state.named_count += 1

        actionable = _is_actionable(node)
        keep = actionable or _has_content(node)
        if not keep:
            return

        if actionable:
            node.element_index = state.next_index
            state.next_index += 1
            state.indexed_nodes.append(node)

        state.nodes.append(node)
        state.markdown_lines.append(_format_markdown_line(node))

    # -------------------------------------------------------------------
    # DFS walker
    # -------------------------------------------------------------------

    def _dfs(
        element: Any,
        depth: int,
        state: _WalkState,
        walker: Any,
        cache_request: Any,
    ) -> None:
        if state.stopped:
            return
        if depth > state.max_depth:
            return
        if state.total_visited >= state.max_elements:
            state.stopped = True
            return

        state.total_visited += 1

        node = _extract_node(element, depth)
        if node is not None:
            try:
                _record_node(node, state)
            except Exception as exc:
                logger.debug("record_node failed: %s", exc)

        # Determine child depth: layout-only containers without text
        # content are "zero cost" — children inherit the same depth
        # budget so a depth=25 walk can reach content 30-50 DOM levels
        # deep inside web / Electron / WebView2 apps.
        if node is not None and _is_layout_only(node) and not _has_content(node):
            child_depth = depth
        else:
            child_depth = depth + 1

        # Walk children
        try:
            child = _com_call(lambda el=element, cr=cache_request: walker.GetFirstChildElementBuildCache(el, cr))
        except COMError as exc:
            logger.debug("GetFirstChildElement COMError: %s", exc)
            child = None
        except Exception as exc:
            logger.debug("GetFirstChildElement failed: %s", exc)
            child = None

        while child is not None:
            if state.stopped or state.total_visited >= state.max_elements:
                state.stopped = True
                return
            try:
                _dfs(child, child_depth, state, walker, cache_request)
            except COMError as exc:
                logger.warning("COMError during DFS at depth %d: %s", depth + 1, exc)
            except Exception as exc:
                logger.warning("Error during DFS at depth %d: %s", depth + 1, exc)

            try:
                child = _com_call(lambda ch=child, cr=cache_request: walker.GetNextSiblingElementBuildCache(ch, cr))
            except COMError as exc:
                logger.debug("GetNextSibling COMError: %s", exc)
                child = None
            except Exception as exc:
                logger.debug("GetNextSibling failed: %s", exc)
                child = None

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def walk_uia_tree(
        hwnd: int,
        query: Optional[str] = None,
        max_depth: int = 25,
        max_elements: int = 5000,
    ) -> UiaTreeResult:
        """Walk the UIA tree rooted at hwnd and produce a UiaTreeResult.

        Per-thread COM initialization (needed when called from executor threads).
        """
        com_initialized = _co_initialize_sta()

        try:
            if UIA is None:
                return UiaTreeResult(
                    tree_markdown="",
                    nodes=[],
                    tree_quality="empty",
                    total_elements=0,
                )

            try:
                uia = get_uia_instance()
            except Exception as exc:
                logger.error("Failed to create CUIAutomation: %s", exc)
                return UiaTreeResult(
                    tree_markdown="",
                    nodes=[],
                    indexed_nodes=[],
                    tree_quality="empty",
                    total_elements=0,
                )

            cache_request = _build_cache_request(uia)

            try:
                root = uia.ElementFromHandleBuildCache(int(hwnd), cache_request)
            except COMError as exc:
                logger.warning("ElementFromHandle(%s) COMError: %s", hwnd, exc)
                root = None
            except Exception as exc:
                logger.warning("ElementFromHandle(%s) error: %s", hwnd, exc)
                root = None

            if root is None:
                return UiaTreeResult(
                    tree_markdown="",
                    nodes=[],
                    indexed_nodes=[],
                    tree_quality="empty",
                    total_elements=0,
                )

            try:
                walker = uia.RawViewWalker
            except Exception as exc:
                logger.error("Failed to obtain RawViewWalker: %s", exc)
                return UiaTreeResult(
                    tree_markdown="",
                    nodes=[],
                    indexed_nodes=[],
                    tree_quality="empty",
                    total_elements=0,
                )

            state = _WalkState(max_depth=max_depth, max_elements=max_elements)
            try:
                _dfs(root, 0, state, walker, cache_request)
            except COMError as exc:
                logger.warning("Top-level DFS COMError: %s", exc)
            except Exception as exc:
                logger.warning("Top-level DFS error: %s", exc)

            markdown = "\n".join(state.markdown_lines)
            if query:
                markdown = filter_tree(markdown, query)

            if state.total_visited == 0:
                quality = "empty"
            else:
                ratio = state.named_count / state.total_visited
                count = state.total_visited
                if ratio > 0.5 and count > 50:
                    quality = "rich"
                elif ratio > 0.15 and count > 15:
                    quality = "ok"
                else:
                    quality = "poor"

            return UiaTreeResult(
                tree_markdown=markdown,
                nodes=state.nodes,
                indexed_nodes=state.indexed_nodes,
                tree_quality=quality,
                total_elements=state.total_visited,
            )
        finally:
            _co_uninitialize_if_needed(com_initialized)


__all__ = [
    "UiaNode",
    "UiaTreeResult",
    "walk_uia_tree",
    "filter_tree",
    "get_uia_instance",
    "invoke_at_point",
    "set_value_at_point",
]
