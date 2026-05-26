"""Per-server mutable runtime context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from computer_server.recording.session import RecordingSession


@dataclass
class ServerContext:
    """Mutable state owned by one server instance."""

    recording_session: Optional[RecordingSession] = None
    learning_recorder: Optional[Any] = None
