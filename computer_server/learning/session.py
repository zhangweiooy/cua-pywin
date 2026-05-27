"""Data types for the learning subsystem.

These types are the OUTPUT of recording. They are NOT recipes — the agent
(Claude Code) converts these into parameterized workflow JSON.

Serialization format is compatible with Ghost OS Swift
LearningDispatch.serializeAction().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LearningError(Enum):
    """Errors specific to learning mode operations."""

    INPUT_MONITORING_NOT_GRANTED = "Input Monitoring permission not granted"
    ALREADY_RECORDING = "Learning is already in progress. Call learn_stop first."
    NOT_RECORDING = "No learning session is active. Call learn_start first."
    HOOK_CREATION_FAILED = "Failed to create Windows hook. Permissions may be stale."
    NO_ACTIONS_RECORDED = "No actions were recorded during the learning session."
    UIA_UNAVAILABLE = "UI Automation COM service is not available."

    def suggestion(self) -> str:
        suggestions = {
            LearningError.INPUT_MONITORING_NOT_GRANTED:
                "On Windows, ensure the application has standard user privileges. "
                "Low-level hooks (WH_KEYBOARD_LL) do not require admin.",
            LearningError.ALREADY_RECORDING:
                "Call computer_learn_stop to end the current session, "
                "or computer_learn_status to check.",
            LearningError.NOT_RECORDING:
                "Call computer_learn_start to begin recording.",
            LearningError.HOOK_CREATION_FAILED:
                "Try restarting the application. If the issue persists, check that "
                "no other application is blocking global hooks.",
            LearningError.NO_ACTIONS_RECORDED:
                "Make sure you performed actions (clicks, typing) while "
                "recording was active.",
            LearningError.UIA_UNAVAILABLE:
                "UI Automation requires COM initialized with "
                "COINIT_APARTMENTTHREADED. Try restarting the application.",
        }
        return suggestions[self]


class ObservedActionType(Enum):
    CLICK = "click"
    TYPE_TEXT = "typeText"
    KEY_PRESS = "keyPress"
    HOTKEY = "hotkey"
    APP_SWITCH = "appSwitch"
    SCROLL = "scroll"
    SECURE_FIELD = "secureField"


@dataclass
class ElementContext:
    """AX/UIA context for the element that was acted upon (e.g., clicked)."""

    role: Optional[str] = None
    title: Optional[str] = None
    identifier: Optional[str] = None
    dom_id: Optional[str] = None
    dom_classes: Optional[str] = None
    computed_name: Optional[str] = None
    parent_role: Optional[str] = None
    vision_label: Optional[str] = None  # YOLO+OCR enrichment at stop time

    def to_dict(self) -> dict:
        result: dict = {}
        for key in (
            "role", "title", "identifier", "dom_id",
            "dom_classes", "computed_name", "parent_role",
            "vision_label",
        ):
            val = getattr(self, key)
            if val is not None:
                result[key] = val
        return result


@dataclass
class ObservedAction:
    """A single observed user action during learning mode.

    Produced by the Windows hook + UIA enrichment pipeline.
    """

    timestamp: int  # time.perf_counter_ns()
    action_type: ObservedActionType
    app_name: str
    app_bundle_id: str | None = None  # Windows: process name
    window_title: str | None = None
    url: str | None = None
    element_context: ElementContext | None = None

    # Payload fields (set depending on action_type)
    x: float | None = None  # click, scroll
    y: float | None = None
    button: str | None = None  # click: "left" / "right"
    count: int | None = None  # click: 1=single, 2=double
    text: str | None = None  # typeText
    key_code: int | None = None  # keyPress
    key_name: str | None = None  # keyPress, hotkey
    modifiers: list[str] | None = None  # keyPress, hotkey
    to_app: str | None = None  # appSwitch
    to_bundle_id: str | None = None  # appSwitch
    delta_x: int | None = None  # scroll
    delta_y: int | None = None  # scroll

    ocr_text: list | None = None
    ocr_image: str | None = None
    ocr_ref_x: int | None = None
    ocr_ref_y: int | None = None
    ocr_region: bytes | None = field(default=None, repr=False)
    ocr_window_x: int | None = None
    ocr_window_y: int | None = None

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict matching Swift LearningDispatch format."""
        d: dict = {
            "timestamp": self.timestamp,
            "action_type": self.action_type.value,
            "app": self.app_name,
            "bundle_id": self.app_bundle_id,
        }
        d["window"] = self.window_title
        d["url"] = self.url
        d["element"] = self.element_context.to_dict() if self.element_context else None

        if self.action_type == ObservedActionType.CLICK:
            if self.x is not None:
                d["x"] = self.x
            if self.y is not None:
                d["y"] = self.y
            if self.button is not None:
                d["button"] = self.button
            if self.count is not None:
                d["count"] = self.count
        elif self.action_type == ObservedActionType.TYPE_TEXT:
            if self.text is not None:
                d["text"] = self.text
            if self.ocr_text is not None:
                d["ocr_text"] = self.ocr_text
            if self.ocr_image is not None:
                d["ocr_image"] = self.ocr_image
            if self.ocr_ref_x is not None:
                d["ocr_ref_x"] = self.ocr_ref_x
            if self.ocr_ref_y is not None:
                d["ocr_ref_y"] = self.ocr_ref_y
        elif self.action_type == ObservedActionType.KEY_PRESS:
            if self.key_code is not None:
                d["key_code"] = self.key_code
            if self.key_name is not None:
                d["key_name"] = self.key_name
            if self.modifiers:
                d["modifiers"] = self.modifiers
        elif self.action_type == ObservedActionType.HOTKEY:
            if self.modifiers:
                d["modifiers"] = self.modifiers
            if self.key_name is not None:
                d["key_name"] = self.key_name
        elif self.action_type == ObservedActionType.APP_SWITCH:
            if self.to_app is not None:
                d["to_app"] = self.to_app
            if self.to_bundle_id is not None:
                d["to_bundle_id"] = self.to_bundle_id
        elif self.action_type == ObservedActionType.SCROLL:
            if self.delta_x is not None:
                d["delta_x"] = self.delta_x
            if self.delta_y is not None:
                d["delta_y"] = self.delta_y
            if self.x is not None:
                d["x"] = self.x
            if self.y is not None:
                d["y"] = self.y
        elif self.action_type == ObservedActionType.SECURE_FIELD:
            d["reason"] = "sensitive_field"
            d["redacted"] = True

        return d


@dataclass
class LearningSession:
    """State of an active learning session. Held by LearningRecorder."""

    task_description: str | None
    start_time: float
    actions: list[ObservedAction] = field(default_factory=list)
    apps: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
