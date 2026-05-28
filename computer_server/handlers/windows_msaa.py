"""Windows MSAA accessibility fallback.

MSAA is older than UI Automation, but some classic Win32/VCL/SAL controls
still expose better roles and labels through IAccessible than through UIA.
This module is intentionally observation-only for now: it returns nodes that
can be merged into ``get_window_state``. Action dispatch through
``accDoDefaultAction`` can be added later if real app coverage justifies it.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class MsaaNode:
    """A single node captured from an MSAA traversal."""

    element_index: Optional[int]
    role: str
    role_id: Optional[int]
    name: Optional[str]
    value: Optional[str] = None
    description: Optional[str] = None
    default_action: Optional[str] = None
    state: Optional[int] = None
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)
    center_x: int = 0
    center_y: int = 0
    actions: List[str] = field(default_factory=list)
    depth: int = 0
    child_id: int = 0


@dataclass
class MsaaTreeResult:
    """Result of a walk_msaa_tree call."""

    tree_markdown: str
    nodes: List[MsaaNode] = field(default_factory=list)
    indexed_nodes: List[MsaaNode] = field(default_factory=list)
    tree_quality: str = "empty"
    total_elements: int = 0


IS_WINDOWS = sys.platform == "win32"


def filter_tree(tree_markdown: str, query: Optional[str]) -> str:
    """Filter a markdown tree, keeping matching lines and their ancestors."""
    if not tree_markdown:
        return ""
    if not query:
        return tree_markdown

    needle = query.lower()
    lines = tree_markdown.splitlines()
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
            if indents[j] < target_indent:
                keep[j] = True
                target_indent = indents[j]
            j -= 1
    return "\n".join(line for i, line in enumerate(lines) if keep[i])


if not IS_WINDOWS:

    def walk_msaa_tree(
        hwnd: int,
        query: Optional[str] = None,
        *,
        index_start: int = 0,
        max_depth: int = 8,
        max_elements: int = 500,
    ) -> MsaaTreeResult:
        return MsaaTreeResult(tree_markdown="", tree_quality="empty")

else:
    import ctypes
    from ctypes import wintypes

    import comtypes
    import comtypes.client
    from comtypes import COMError
    from comtypes.automation import VARIANT, VT_DISPATCH, VT_I4

    try:
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen import Accessibility  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local COM registry
        logger.warning("Failed to load oleacc type library: %s", exc)
        Accessibility = None  # type: ignore[assignment]

    OBJID_CLIENT = ctypes.c_long(-4).value
    CHILDID_SELF = 0
    IID_IAccessible = comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")

    ROLE_NAMES = {
        1: "TitleBar",
        2: "MenuBar",
        3: "ScrollBar",
        9: "Window",
        10: "Client",
        11: "MenuPopup",
        12: "MenuItem",
        13: "ToolTip",
        14: "Application",
        15: "Document",
        16: "Pane",
        18: "Dialog",
        20: "Group",
        21: "Separator",
        22: "ToolBar",
        23: "StatusBar",
        24: "Table",
        25: "ColumnHeader",
        26: "RowHeader",
        29: "Cell",
        30: "Link",
        33: "List",
        34: "ListItem",
        35: "Tree",
        36: "TreeItem",
        37: "TabItem",
        38: "PropertyPage",
        40: "Image",
        41: "Text",
        42: "Edit",
        43: "Button",
        44: "CheckBox",
        45: "RadioButton",
        46: "ComboBox",
        47: "DropList",
        48: "ProgressBar",
        50: "HotKeyField",
        51: "Slider",
        52: "SpinButton",
        56: "ButtonDropDown",
        57: "ButtonMenu",
        58: "ButtonDropDownGrid",
        60: "Tab",
        62: "SplitButton",
        64: "OutlineButton",
    }

    ACTIONABLE_ROLES = {
        "Button",
        "ButtonDropDown",
        "ButtonMenu",
        "ButtonDropDownGrid",
        "CheckBox",
        "ComboBox",
        "DropList",
        "Edit",
        "Link",
        "ListItem",
        "MenuItem",
        "RadioButton",
        "Slider",
        "SpinButton",
        "SplitButton",
        "TabItem",
        "TreeItem",
    }
    DROPDOWN_ROLE_IDS = {56, 57, 58, 62}
    CLICKABLE_ROLE_IDS = {12, 30, 34, 37, 43, 44, 45, 46, 47}

    def _str_or_none(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            text = str(value)
        except Exception:
            return None
        text = text.strip()
        return text or None

    def _variant_child(value: int = CHILDID_SELF) -> VARIANT:
        variant = VARIANT()
        variant.value = int(value)
        return variant

    def _role_name(role: Any) -> str:
        if isinstance(role, int):
            return ROLE_NAMES.get(role, f"Role{role}")
        return _str_or_none(role) or "Unknown"

    def _role_id(role: Any) -> Optional[int]:
        return role if isinstance(role, int) else None

    def _safe_prop(fn, default: Any = None) -> Any:
        try:
            return fn()
        except (COMError, AttributeError, OSError, ValueError, TypeError):
            return default
        except Exception:
            return default

    def _get_location(acc: Any, child_id: int) -> Tuple[int, int, int, int]:
        child = _variant_child(child_id)
        try:
            left, top, width, height = acc.accLocation(child)
        except Exception:
            return (0, 0, 0, 0)
        try:
            left = int(left)
            top = int(top)
            width = int(width)
            height = int(height)
        except Exception:
            return (0, 0, 0, 0)
        if width <= 0 or height <= 0:
            return (0, 0, 0, 0)
        return (left, top, left + width, top + height)

    def _accessible_from_window(hwnd: int) -> Any:
        if Accessibility is None:
            return None
        oleacc = ctypes.oledll.oleacc
        ptr = ctypes.POINTER(Accessibility.IAccessible)()
        hr = oleacc.AccessibleObjectFromWindow(
            wintypes.HWND(int(hwnd)),
            ctypes.c_ulong(OBJID_CLIENT),
            ctypes.byref(IID_IAccessible),
            ctypes.byref(ptr),
        )
        if hr != 0:
            return None
        return ptr

    def _accessible_children(acc: Any) -> List[Tuple[Any, int]]:
        count = int(_safe_prop(lambda: acc.accChildCount, 0) or 0)
        if count <= 0:
            return []
        children = (VARIANT * count)()
        obtained = ctypes.c_long()
        try:
            ctypes.oledll.oleacc.AccessibleChildren(
                acc,
                0,
                count,
                children,
                ctypes.byref(obtained),
            )
        except Exception:
            return []

        result: List[Tuple[Any, int]] = []
        for i in range(int(obtained.value)):
            child = children[i]
            if child.vt == VT_DISPATCH and child.value is not None:
                child_acc = _safe_prop(
                    lambda ch=child: ch.value.QueryInterface(Accessibility.IAccessible)
                )
                if child_acc is not None:
                    result.append((child_acc, CHILDID_SELF))
            elif child.vt == VT_I4:
                result.append((acc, int(child.value)))
        return result

    def _extract_node(acc: Any, child_id: int, depth: int) -> Optional[MsaaNode]:
        child = _variant_child(child_id)
        raw_role = _safe_prop(lambda: acc.accRole(child))
        role_id = _role_id(raw_role)
        role = _role_name(raw_role)
        name = _str_or_none(_safe_prop(lambda: acc.accName(child)))
        value = _str_or_none(_safe_prop(lambda: acc.accValue(child)))
        description = _str_or_none(_safe_prop(lambda: acc.accDescription(child)))
        default_action = _str_or_none(_safe_prop(lambda: acc.accDefaultAction(child)))
        state = _safe_prop(lambda: acc.accState(child))
        bbox = _get_location(acc, child_id)
        center_x = (bbox[0] + bbox[2]) // 2
        center_y = (bbox[1] + bbox[3]) // 2

        actions: List[str] = []
        if default_action or role in ACTIONABLE_ROLES or role_id in CLICKABLE_ROLE_IDS:
            actions.append("invoke")
        if role_id in DROPDOWN_ROLE_IDS:
            if "invoke" not in actions:
                actions.append("invoke")
            actions.append("expand")
        if role == "Edit":
            actions.append("set_value")

        if not (name or value or description or actions):
            return None

        return MsaaNode(
            element_index=None,
            role=role,
            role_id=role_id,
            name=name,
            value=value,
            description=description,
            default_action=default_action,
            state=int(state) if isinstance(state, int) else None,
            bbox=bbox,
            center_x=center_x,
            center_y=center_y,
            actions=actions,
            depth=depth,
            child_id=child_id,
        )

    def _format_markdown_line(node: MsaaNode) -> str:
        indent = "  " * node.depth
        parts: List[str] = []
        if node.element_index is not None:
            parts.append(f"[{node.element_index}]")
        parts.append(node.role)
        if node.name:
            parts.append(f'"{node.name.replace(chr(34), chr(92) + chr(34))}"')
        if node.value:
            parts.append(f'="{node.value.replace(chr(34), chr(92) + chr(34))}"')
        meta: List[str] = []
        if node.default_action:
            meta.append(f"default_action={node.default_action}")
        if node.actions:
            meta.append(f"actions=[{','.join(node.actions)}]")
        if meta:
            parts.append("[" + ", ".join(meta) + "]")
        return f"{indent}{' '.join(parts)}"

    def _is_actionable(node: MsaaNode) -> bool:
        if not node.actions:
            return False
        if node.role not in ACTIONABLE_ROLES and not (
            node.name or node.value or node.description
        ):
            return False
        return node.bbox != (0, 0, 0, 0)

    def walk_msaa_tree(
        hwnd: int,
        query: Optional[str] = None,
        *,
        index_start: int = 0,
        max_depth: int = 8,
        max_elements: int = 500,
    ) -> MsaaTreeResult:
        """Walk the MSAA tree rooted at hwnd and produce a MsaaTreeResult."""
        com_initialized = False
        try:
            comtypes.CoInitialize()
            com_initialized = True
        except OSError:
            pass

        try:
            root = _accessible_from_window(hwnd)
            if root is None:
                return MsaaTreeResult(tree_markdown="", tree_quality="empty")

            nodes: List[MsaaNode] = []
            indexed_nodes: List[MsaaNode] = []
            markdown_lines: List[str] = []
            seen: Set[Tuple[int, int]] = set()
            total = 0
            next_index = int(index_start)

            def visit(acc: Any, child_id: int, depth: int) -> None:
                nonlocal total, next_index
                if total >= max_elements or depth > max_depth:
                    return
                key = (id(acc), child_id)
                if key in seen:
                    return
                seen.add(key)
                total += 1

                node = _extract_node(acc, child_id, depth)
                if node is not None:
                    if _is_actionable(node):
                        node.element_index = next_index
                        next_index += 1
                        indexed_nodes.append(node)
                    nodes.append(node)
                    markdown_lines.append(_format_markdown_line(node))

                if child_id != CHILDID_SELF:
                    return
                for child_acc, next_child_id in _accessible_children(acc):
                    visit(child_acc, next_child_id, depth + 1)

            visit(root, CHILDID_SELF, 0)
            markdown = "\n".join(markdown_lines)
            if query:
                markdown = filter_tree(markdown, query)

            useful_count = len(indexed_nodes)
            if useful_count == 0:
                quality = "empty"
            elif useful_count > 50:
                quality = "rich"
            elif useful_count > 15:
                quality = "ok"
            else:
                quality = "poor"

            return MsaaTreeResult(
                tree_markdown=markdown,
                nodes=nodes,
                indexed_nodes=indexed_nodes,
                tree_quality=quality,
                total_elements=total,
            )
        except Exception as exc:
            logger.debug("MSAA tree walk failed: %s", exc, exc_info=True)
            return MsaaTreeResult(tree_markdown="", tree_quality="empty")
        finally:
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass


__all__ = ["MsaaNode", "MsaaTreeResult", "walk_msaa_tree"]
