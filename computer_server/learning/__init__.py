"""CUA Learning — Windows user action recorder with UIA enrichment.

Records human user actions (keyboard, mouse clicks, scroll) via pynput
global hooks, enriched with UI Automation element metadata at interaction
points. The recorded actions can be fed to an AI agent for workflow
synthesis (recipe generation).

Target platform: Windows (Python >= 3.10).

Requires: pynput, uiautomation, pywin32, psutil (all Windows-only).
"""

from __future__ import annotations

from .session import (
    ElementContext,
    LearningError,
    LearningSession,
    ObservedAction,
    ObservedActionType,
)
from .coalescer import KeystrokeBuncher, ScrollBuncher
from .recorder import LearningRecorder

__all__ = [
    "ElementContext",
    "KeystrokeBuncher",
    "LearningError",
    "LearningRecorder",
    "LearningSession",
    "ObservedAction",
    "ObservedActionType",
    "ScrollBuncher",
]
