"""Data types for the learning subsystem.

These types are the OUTPUT of recording. They are NOT recipes — the agent
(Claude Code) converts these into parameterized workflow JSON.

The public action serialization is the strict DesktopAction protocol consumed
by the Oryx Desktop adapter. Internal observation data remains separate from
that protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


TEACHING_CONTRACT = "desktop-action.v1"
TEACHING_STOP_BARRIER = "cutoff-sequence.v1"


class LearningError(Enum):
    """Errors specific to learning mode operations."""

    INPUT_MONITORING_NOT_GRANTED = "Input Monitoring permission not granted"
    ALREADY_RECORDING = (
        "Learning is already in progress. Stop it with its active session_id first."
    )
    NOT_RECORDING = "No learning session is active."
    SESSION_MISMATCH = "session_id does not match the learning session"
    HOOK_CREATION_FAILED = "Failed to create Windows hook. Permissions may be stale."
    NO_ACTIONS_RECORDED = "No actions were recorded during the learning session."
    UIA_UNAVAILABLE = "UI Automation COM service is not available."

    def suggestion(self) -> str:
        suggestions = {
            LearningError.INPUT_MONITORING_NOT_GRANTED:
                "On Windows, ensure the application has standard user privileges. "
                "Low-level hooks (WH_KEYBOARD_LL) do not require admin.",
            LearningError.ALREADY_RECORDING:
                "Call computer_learn(action='status') to inspect the active session, "
                "then stop it with the returned session_id.",
            LearningError.NOT_RECORDING:
                "Call computer_learn(action='start') with a session_id to begin "
                "recording.",
            LearningError.SESSION_MISMATCH:
                "Use the same session_id that started the learning session.",
            LearningError.HOOK_CREATION_FAILED:
                "Try restarting the application. If the issue persists, check that "
                "no other application is blocking global hooks.",
            LearningError.NO_ACTIONS_RECORDED:
                "Make sure you performed actions (clicks, typing) while "
                "recording was active.",
            LearningError.UIA_UNAVAILABLE:
                "The learning UI Automation STA worker could not initialize "
                "or query the Windows UI Automation root. Check daemon logs "
                "and Windows UI Automation availability.",
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
    sequence: int | None = None
    occurred_at_epoch_ms: int | None = None
    source_window_id: str | None = None
    app_bundle_id: str | None = None  # Windows: process name
    window_title: str | None = None
    url: str | None = None
    element_context: ElementContext | None = None
    action_id: str | None = None
    correlation_id: str | None = None
    before_observation_id: str | None = None
    after_observation_id: str | None = None

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
        """Serialize one strict DesktopAction.

        Sequence, wall-clock time, and native window identity are assigned at
        the recorder ingestion boundary. Emitting a partial action is a
        protocol error rather than an invitation for consumers to guess.
        """
        if self.action_id is None:
            raise ValueError("DesktopAction.eventId is required")
        if not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("DesktopAction.sequence must be a positive integer")
        if (
            not isinstance(self.occurred_at_epoch_ms, int)
            or self.occurred_at_epoch_ms <= 0
        ):
            raise ValueError(
                "DesktopAction.occurredAtEpochMs must be a positive Epoch millisecond"
            )
        if not isinstance(self.source_window_id, str) or not self.source_window_id:
            raise ValueError("DesktopAction.sourceWindowId is required")

        payload: dict[str, Any] = {
            "app": self.app_name,
            "processName": self.app_bundle_id,
            "windowTitle": self.window_title,
            "url": self.url,
            "element": (
                self.element_context.to_dict()
                if self.element_context
                else None
            ),
        }

        if self.action_type == ObservedActionType.CLICK:
            if self.x is not None:
                payload["x"] = self.x
            if self.y is not None:
                payload["y"] = self.y
            if self.button is not None:
                payload["button"] = self.button
            if self.count is not None:
                payload["count"] = self.count
        elif self.action_type == ObservedActionType.TYPE_TEXT:
            if self.text is not None:
                payload["text"] = self.text
            if self.ocr_text is not None:
                payload["ocrText"] = self.ocr_text
            if self.ocr_image is not None:
                payload["ocrImage"] = self.ocr_image
            if self.ocr_ref_x is not None:
                payload["ocrRefX"] = self.ocr_ref_x
            if self.ocr_ref_y is not None:
                payload["ocrRefY"] = self.ocr_ref_y
        elif self.action_type == ObservedActionType.KEY_PRESS:
            if self.key_code is not None:
                payload["keyCode"] = self.key_code
            if self.key_name is not None:
                payload["keyName"] = self.key_name
            if self.modifiers:
                payload["modifiers"] = self.modifiers
        elif self.action_type == ObservedActionType.HOTKEY:
            if self.modifiers:
                payload["modifiers"] = self.modifiers
            if self.key_name is not None:
                payload["keyName"] = self.key_name
        elif self.action_type == ObservedActionType.APP_SWITCH:
            if self.to_app is not None:
                payload["toApp"] = self.to_app
            if self.to_bundle_id is not None:
                payload["toProcessName"] = self.to_bundle_id
        elif self.action_type == ObservedActionType.SCROLL:
            if self.delta_x is not None:
                payload["deltaX"] = self.delta_x
            if self.delta_y is not None:
                payload["deltaY"] = self.delta_y
            if self.x is not None:
                payload["x"] = self.x
            if self.y is not None:
                payload["y"] = self.y
        elif self.action_type == ObservedActionType.SECURE_FIELD:
            payload["reason"] = "sensitive_field"
            payload["redacted"] = True

        return {
            "eventId": self.action_id,
            "sequence": self.sequence,
            "occurredAtEpochMs": self.occurred_at_epoch_ms,
            "sourceWindowId": self.source_window_id,
            "actionType": self.action_type.value,
            "payload": payload,
        }


@dataclass
class LearningObservation:
    """A lightweight semantic snapshot attached to a learned action."""

    observation_id: str
    trigger: str
    timestamp: int
    correlation_id: str | None = None
    app_name: str | None = None
    app_bundle_id: str | None = None
    window_title: str | None = None
    url: str | None = None
    element_context: ElementContext | None = None
    state_text: str | None = None
    semantic_nodes: list[dict[str, Any]] = field(default_factory=list)
    tree_quality: str | None = None
    capture_backend: str | None = None
    target_profile: dict[str, Any] | None = None
    capture_reliability: str | None = None
    semantic_degraded_reason: str | None = None
    delivery_risk: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    skip_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "observation_id": self.observation_id,
            "trigger": self.trigger,
            "timestamp": self.timestamp,
        }
        optional_values = {
            "correlation_id": self.correlation_id,
            "app": self.app_name,
            "bundle_id": self.app_bundle_id,
            "window": self.window_title,
            "url": self.url,
            "state_text": self.state_text,
            "tree_quality": self.tree_quality,
            "capture_backend": self.capture_backend,
            "capture_reliability": self.capture_reliability,
            "semantic_degraded_reason": self.semantic_degraded_reason,
            "delivery_risk": self.delivery_risk,
            "skip_reason": self.skip_reason,
            "error": self.error,
        }
        for key, value in optional_values.items():
            if value is not None:
                d[key] = value
        if self.target_profile is not None:
            d["target_profile"] = self.target_profile
        if self.element_context is not None:
            d["element"] = self.element_context.to_dict()
        if self.semantic_nodes:
            d["semantic_nodes"] = self.semantic_nodes
        if self.timing:
            d["timing"] = self.timing
        return d


@dataclass
class LearningLink:
    """Relationship between an observed action and its semantic snapshot."""

    action_id: str | None
    observation_id: str
    correlation_id: str | None
    trigger: str
    relation: str = "after"

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "observation_id": self.observation_id,
            "trigger": self.trigger,
            "relation": self.relation,
        }
        if self.action_id is not None:
            d["action_id"] = self.action_id
        if self.correlation_id is not None:
            d["correlation_id"] = self.correlation_id
        return d


def default_learning_metrics() -> dict[str, Any]:
    return {
        "action_count": 0,
        "observation_count": 0,
        "pending_observations": 0,
        "dropped_observations": 0,
        "timeout_count": 0,
        "sensitive_skip_count": 0,
        "error_count": 0,
        "degraded_observations": 0,
        "capture_warning_count": 0,
        "target_profile_counts": {},
        "last_event_at": None,
        "last_observation_at": None,
    }


@dataclass
class LearningSession:
    """State of an active learning session. Held by LearningRecorder."""

    session_id: str
    task_description: str | None
    start_time: float
    next_sequence: int = 1
    cutoff_sequence: int | None = None
    end_time: float | None = None
    actions: list[ObservedAction] = field(default_factory=list)
    observations: list[LearningObservation] = field(default_factory=list)
    links: list[LearningLink] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=default_learning_metrics)
    apps: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)

    def to_dict(
        self,
        *,
        duration_seconds: int,
        actions: list[ObservedAction] | None = None,
    ) -> dict:
        action_list = actions if actions is not None else self.actions
        metrics = dict(default_learning_metrics())
        metrics.update(self.metrics)
        metrics["action_count"] = len(action_list)
        metrics["observation_count"] = len(self.observations)
        return {
            "task_description": self.task_description or "",
            "duration_seconds": int(duration_seconds),
            "action_count": len(action_list),
            "apps": list(self.apps),
            "urls": list(self.urls),
            "actions": [action.to_dict() for action in action_list],
            "observations": [observation.to_dict() for observation in self.observations],
            "links": [link.to_dict() for link in self.links],
            "metrics": metrics,
        }
