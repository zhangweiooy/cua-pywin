"""Per-server mutable runtime context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from computer_server.recording.session import RecordingSession
from computer_server.services.window_state_service import UIAWalkGate


@dataclass
class ServerContext:
    """Mutable state owned by one server instance.

    Anything that would otherwise live as a module-level singleton goes here.
    This keeps multiple FastMCP instances (stdio + HTTP, tests, etc.) from
    sharing state through ``computer_server.services.*`` module globals.
    """

    recording_session: Optional[RecordingSession] = None
    learning_recorder: Optional[Any] = None
    uia_walk_gate: UIAWalkGate = field(default_factory=UIAWalkGate)
    seen_popovers: Dict[int, Set[int]] = field(default_factory=dict)
