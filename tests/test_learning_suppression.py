from __future__ import annotations

import time

from computer_server.learning.recorder import LearningRecorder
from computer_server.learning.session import LearningSession


class _FakeBuncher:
    def __init__(self):
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1


def test_suppress_agent_input_flushes_pending_user_input():
    recorder = LearningRecorder()
    key_buncher = _FakeBuncher()
    scroll_buncher = _FakeBuncher()
    recorder._keystroke_buncher = key_buncher
    recorder._scroll_buncher = scroll_buncher

    recorder.suppress_agent_input(0.1)

    assert key_buncher.flush_count == 1
    assert scroll_buncher.flush_count == 1


def test_suppressed_mouse_event_is_not_recorded():
    recorder = LearningRecorder()
    recorder._session = LearningSession(
        session_id="session-1",
        task_description=None,
        start_time=time.time(),
    )
    recorder.suppress_agent_input(1.0)

    recorder._on_mouse_down(x=10, y=20, button="left", count=1)

    assert recorder._session.actions == []


def test_scoped_agent_input_suppression_blocks_until_ended():
    recorder = LearningRecorder()
    recorder._session = LearningSession(
        session_id="session-1",
        task_description=None,
        start_time=time.time(),
    )

    recorder.begin_agent_input_suppression()
    assert recorder._drop_agent_input_event() is True

    recorder.end_agent_input_suppression(grace_seconds=0)
    assert recorder._drop_agent_input_event() is False


def test_clear_agent_input_suppression_removes_scoped_state():
    recorder = LearningRecorder()
    recorder._session = LearningSession(
        session_id="session-1",
        task_description=None,
        start_time=time.time(),
    )

    recorder.begin_agent_input_suppression()
    recorder.clear_agent_input_suppression()

    assert recorder._drop_agent_input_event() is False
