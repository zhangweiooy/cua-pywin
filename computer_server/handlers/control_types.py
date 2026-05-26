"""Shared UIA control-type normalization."""

from __future__ import annotations

from typing import Any, Dict, Optional


CONTROL_TYPE_NAMES: Dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
    50039: "SemanticZoom",
    50040: "AppBar",
}

SEMANTIC_ROLES: Dict[str, str] = {
    "Button": "AXButton",
    "Calendar": "AXGroup",
    "CheckBox": "AXCheckBox",
    "ComboBox": "AXComboBox",
    "Edit": "AXTextField",
    "Hyperlink": "AXLink",
    "Image": "AXImage",
    "ListItem": "AXStaticText",
    "List": "AXList",
    "Menu": "AXMenu",
    "MenuItem": "AXMenuItem",
    "ProgressBar": "AXProgressIndicator",
    "RadioButton": "AXRadioButton",
    "ScrollBar": "AXScrollBar",
    "Slider": "AXSlider",
    "Spinner": "AXTextField",
    "Tab": "AXTab",
    "TabItem": "AXRadioButton",
    "Text": "AXStaticText",
    "ToolBar": "AXToolbar",
    "Tree": "AXOutline",
    "TreeItem": "AXRow",
    "Thumb": "AXValueIndicator",
    "Window": "AXWindow",
    "Pane": "AXGroup",
    "Group": "AXGroup",
    "Document": "AXGroup",
    "DataGrid": "AXTable",
    "Header": "AXStaticText",
    "HeaderItem": "AXStaticText",
    "SplitButton": "AXPopUpButton",
    "SemanticZoom": "AXGroup",
    "AppBar": "AXToolbar",
}


def uia_control_type_name(value: Any) -> str:
    """Return canonical UIA control type name."""
    if value is None:
        return "Unknown"
    text = str(value)
    if text.endswith("ControlType"):
        return text[: -len("ControlType")]
    try:
        cid = int(value)
    except (TypeError, ValueError):
        return text or "Unknown"
    return CONTROL_TYPE_NAMES.get(cid, f"Custom({cid})")


def semantic_role(value: Any) -> Optional[str]:
    """Return AX-style semantic role for a UIA control type."""
    name = uia_control_type_name(value)
    return SEMANTIC_ROLES.get(name)
