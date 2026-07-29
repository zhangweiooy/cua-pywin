from __future__ import annotations

import queue
import threading
import time

from computer_server.learning.recorder import LearningRecorder, _ObservationJob
from computer_server.learning.session import (
    LearningObservation,
    LearningError,
    LearningSession,
    ObservedAction,
    ObservedActionType,
)


class _FakeInput:
    def is_healthy(self):
        return True


class _FakeAppDetector:
    def current_app_info(self):
        return ("Notepad", "notepad.exe", "Untitled - Notepad")

    def current_app_context(self):
        return ("Notepad", "notepad.exe", "Untitled - Notepad", "1001")


class _FakeUIA:
    def is_password_field_focused(self):
        return False

    def focused_element_context(self):
        return {"role": "edit", "title": "Document"}

    def enrich_click(self, x, y):
        return {"role": "button", "title": f"Button {int(x)},{int(y)}"}


class _SwitchingAppDetector:
    def __init__(self):
        self.context = ("App A", "a.exe", "A", "1001")

    def current_app_info(self):
        return self.context[:3]

    def current_app_context(self):
        return self.context

    def check_app_switch_with_info(self, *_args):
        return False


def _recorder_with_session() -> LearningRecorder:
    recorder = LearningRecorder()
    recorder._session = LearningSession(
        session_id="session-1",
        task_description=None,
        start_time=time.time(),
    )
    return recorder


def _track_stop_waits(recorder: LearningRecorder) -> threading.Event:
    waiter_started = threading.Event()

    class _TrackingCondition(threading.Condition):
        def wait(self, timeout=None):
            waiter_started.set()
            return super().wait(timeout)

    recorder._stop_condition = _TrackingCondition(recorder._lock)
    return waiter_started


def test_append_assigns_ids_and_queues_observation_job():
    recorder = _recorder_with_session()
    recorder._observation_queue = queue.Queue(maxsize=4)
    action = ObservedAction(
        timestamp=10,
        action_type=ObservedActionType.CLICK,
        app_name="Notepad",
        source_window_id="1001",
        app_bundle_id="notepad.exe",
        window_title="Untitled - Notepad",
        x=10,
        y=20,
    )

    assert recorder._append(action, observation_trigger="click") is True

    assert action.action_id
    assert action.correlation_id
    assert action.sequence == 1
    assert action.occurred_at_epoch_ms is not None
    assert recorder._session is not None
    assert recorder._session.actions == [action]
    assert recorder._session.metrics["action_count"] == 1
    assert recorder._session.metrics["pending_observations"] == 1
    job = recorder._observation_queue.get_nowait()
    assert job.action_id == action.action_id
    assert job.correlation_id == action.correlation_id
    assert job.trigger == "click"


def test_window_switch_does_not_enrich_old_keystrokes_from_new_focus():
    from computer_server.learning.coalescer import KeystrokeBuncher

    recorder = _recorder_with_session()
    detector = _SwitchingAppDetector()
    focused_values = iter(["A-before", "B-before", "B-before你"])

    class UIA(_FakeUIA):
        def read_focused_value(self):
            return next(focused_values)

    recorder._app_detector = detector
    recorder._uia = UIA()
    recorder._uia_gate.run_with_timeout = lambda func, timeout: func()
    recorder._keystroke_buncher = KeystrokeBuncher(
        recorder._on_keystroke_flush
    )

    recorder._on_key_down(65, "a", [], False)
    detector.context = ("App B", "b.exe", "B", "9001")
    recorder._on_key_down(66, "b", [], False)
    recorder._keystroke_buncher.flush()

    assert recorder._session is not None
    assert [
        (action.text, action.source_window_id)
        for action in recorder._session.actions
    ] == [
        ("a", "1001"),
        ("你", "9001"),
    ]


def test_complete_observation_links_action_and_updates_metrics():
    recorder = _recorder_with_session()
    action = ObservedAction(
        timestamp=10,
        action_type=ObservedActionType.CLICK,
        app_name="Notepad",
        action_id="act_1",
        correlation_id="corr_1",
    )
    assert recorder._session is not None
    recorder._session.actions.append(action)
    recorder._session.metrics["pending_observations"] = 1
    job = _ObservationJob(
        trigger="click",
        timestamp=11,
        correlation_id="corr_1",
        action_id="act_1",
    )
    observation = LearningObservation(
        observation_id="obs_1",
        correlation_id="corr_1",
        trigger="click",
        timestamp=12,
        tree_quality="poor",
        capture_backend="none",
    )

    recorder._complete_observation(job, observation)

    assert action.after_observation_id == "obs_1"
    assert recorder._session.observations == [observation]
    assert recorder._session.links[0].to_dict() == {
        "observation_id": "obs_1",
        "trigger": "click",
        "relation": "after",
        "action_id": "act_1",
        "correlation_id": "corr_1",
    }
    assert recorder._session.metrics["pending_observations"] == 0
    assert recorder._session.metrics["observation_count"] == 1
    assert recorder._session.metrics["last_observation_at"] == 12
    assert recorder._session.metrics["degraded_observations"] == 1
    assert recorder._session.metrics["target_profile_counts"] == {"unknown": 1}


def test_build_observation_uses_lightweight_uia_context():
    recorder = _recorder_with_session()
    recorder._uia = _FakeUIA()
    recorder._app_detector = _FakeAppDetector()
    job = _ObservationJob(
        trigger="typing_pause",
        timestamp=11,
        correlation_id="corr_1",
        app_bundle_id="notepad.exe",
    )

    observation = recorder._build_observation(job)

    assert observation.capture_backend == "none"
    assert observation.capture_reliability == "not_captured"
    assert observation.tree_quality == "poor"
    assert observation.semantic_degraded_reason == "lightweight_semantic_context"
    assert observation.target_profile["family"] == "generic"
    assert observation.target_profile["process_name"] == "notepad.exe"
    assert observation.delivery_risk == "focused_input_required"
    assert observation.semantic_nodes[0]["role"] == "edit"
    assert observation.semantic_nodes[0]["source"] == "uia"
    assert "Window: Notepad" in observation.state_text


def test_wait_for_observation_idle_marks_pending_as_dropped_on_timeout():
    recorder = _recorder_with_session()
    assert recorder._session is not None
    recorder._session.metrics["pending_observations"] = 2

    assert recorder._wait_for_observation_idle(0.0) is False

    assert recorder._session.metrics["pending_observations"] == 0
    assert recorder._session.metrics["dropped_observations"] == 2


def test_status_dict_reports_additive_timeline_metrics():
    recorder = _recorder_with_session()
    recorder._input = _FakeInput()
    recorder._app_detector = _FakeAppDetector()
    assert recorder._session is not None
    recorder._session.metrics["pending_observations"] = 1
    recorder._session.metrics["timeout_count"] = 2
    recorder._session.metrics["sensitive_skip_count"] = 3

    data = recorder.status_dict()

    assert data["recording"] is True
    assert data["mode"] == "reduced"
    assert data["action_count"] == 0
    assert data["observation_count"] == 0
    assert data["pending_observations"] == 1
    assert data["timeout_count"] == 2
    assert data["sensitive_skip_count"] == 3
    assert data["degraded_observations"] == 0
    assert data["capture_warning_count"] == 0
    assert data["target_profile_counts"] == {}
    assert data["current_app"] == "Notepad"
    assert data["listener_healthy"] is True
    assert data["actions"] == []
    assert data["session_id"] == "session-1"
    assert data["next_sequence"] == 0
    assert data["timeline_truncated"] is False


def test_status_dict_returns_bounded_incremental_timeline():
    recorder = _recorder_with_session()
    assert recorder._session is not None
    recorder._session.actions.extend([
        ObservedAction(
            action_id=f"action_{index}",
            action_type=ObservedActionType.CLICK,
            app_name="Notepad",
            timestamp=index,
            sequence=index + 1,
            occurred_at_epoch_ms=1_725_000_000_000 + index,
            source_window_id="1001",
            x=index,
            y=index,
        )
        for index in range(3)
    ])
    first = recorder.status_dict(timeline_limit=2)

    assert [item["eventId"] for item in first["actions"]] == [
        "action_0",
        "action_1",
    ]
    assert first["next_sequence"] == 2
    assert first["timeline_truncated"] is True

    second = recorder.status_dict(
        after_sequence=first["next_sequence"],
        timeline_limit=2,
    )

    assert [item["eventId"] for item in second["actions"]] == ["action_2"]
    assert second["next_sequence"] == 3
    assert second["timeline_truncated"] is False


def test_append_rejects_missing_source_window_and_post_cutoff_action():
    recorder = _recorder_with_session()
    assert recorder._session is not None
    missing_source = ObservedAction(
        timestamp=1,
        action_type=ObservedActionType.CLICK,
        app_name="Unknown",
        source_window_id="",
    )
    late = ObservedAction(
        timestamp=2,
        action_type=ObservedActionType.CLICK,
        app_name="Notepad",
        source_window_id="1001",
    )

    assert recorder._append(missing_source) is False
    recorder._stopping = True
    assert recorder._append(late) is False
    assert recorder._session.actions == []


def test_observation_skip_and_error_counters_are_non_fatal():
    recorder = _recorder_with_session()
    assert recorder._session is not None
    recorder._session.metrics["pending_observations"] = 2

    recorder._complete_observation(
        _ObservationJob(trigger="click", timestamp=1, correlation_id="corr_1"),
        LearningObservation(
            observation_id="obs_1",
            correlation_id="corr_1",
            trigger="click",
            timestamp=2,
            skip_reason="timeout",
        ),
    )
    recorder._complete_observation(
        _ObservationJob(trigger="click", timestamp=3, correlation_id="corr_2"),
        LearningObservation(
            observation_id="obs_2",
            correlation_id="corr_2",
            trigger="click",
            timestamp=4,
            skip_reason="sensitive_context",
            error="redacted",
        ),
    )

    assert recorder._session.metrics["pending_observations"] == 0
    assert recorder._session.metrics["timeout_count"] == 1
    assert recorder._session.metrics["sensitive_skip_count"] == 1
    assert recorder._session.metrics["error_count"] == 1
    assert recorder._session.metrics["degraded_observations"] == 2
    assert recorder._session.metrics["target_profile_counts"] == {"unknown": 2}


def test_repeated_stop_returns_same_completed_session(monkeypatch):
    recorder = _recorder_with_session()
    assert recorder._session is not None
    assert recorder._append(
        ObservedAction(
            timestamp=10,
            action_type=ObservedActionType.CLICK,
            app_name="Notepad",
            source_window_id="1001",
        )
    )
    monkeypatch.setattr(recorder, "_enrich_vision", lambda actions: None)

    first = recorder.stop()
    second = recorder.stop()

    assert first[0] is True
    assert second == first
    assert recorder.is_recording is False
    assert first[1][0].end_time is not None


def test_repeated_stop_rejects_a_different_session_id(monkeypatch):
    recorder = _recorder_with_session()
    monkeypatch.setattr(recorder, "_enrich_vision", lambda actions: None)

    first = recorder.stop("session-1")
    mismatched = recorder.stop("session-2")

    assert first[0] is True
    assert mismatched == (False, LearningError.SESSION_MISMATCH)


def test_concurrent_stop_waits_for_same_completed_session(monkeypatch):
    recorder = _recorder_with_session()
    waiter_started = _track_stop_waits(recorder)
    assert recorder._session is not None
    session = recorder._session
    expected = (True, (session, session.actions))
    session_detached = threading.Event()
    release_stop = threading.Event()

    def blocked_finish_stop():
        with recorder._lock:
            recorder._session = None
        session_detached.set()
        assert release_stop.wait(timeout=2)
        return expected

    monkeypatch.setattr(recorder, "_finish_stop", blocked_finish_stop)
    first_results = []
    second_results = []
    first = threading.Thread(target=lambda: first_results.append(recorder.stop()))
    second = threading.Thread(target=lambda: second_results.append(recorder.stop()))

    first.start()
    assert session_detached.wait(timeout=2)
    second.start()

    assert waiter_started.wait(timeout=2)
    assert recorder.start(
        session_id="session-2",
    ) is LearningError.ALREADY_RECORDING

    release_stop.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert first_results == [expected]
    assert second_results == [expected]


def test_manual_stop_waits_for_max_duration_result(monkeypatch):
    recorder = _recorder_with_session()
    waiter_started = _track_stop_waits(recorder)
    assert recorder._session is not None
    session = recorder._session
    expected = (True, (session, session.actions))
    stop_started = threading.Event()
    release_stop = threading.Event()

    def blocked_finish_stop():
        stop_started.set()
        assert release_stop.wait(timeout=2)
        with recorder._lock:
            recorder._session = None
        return expected

    monkeypatch.setattr(recorder, "_finish_stop", blocked_finish_stop)
    timer = threading.Thread(target=recorder._on_max_duration)
    manual_results = []
    manual = threading.Thread(target=lambda: manual_results.append(recorder.stop()))

    timer.start()
    assert stop_started.wait(timeout=2)
    manual.start()

    assert waiter_started.wait(timeout=2)

    release_stop.set()
    timer.join(timeout=2)
    manual.join(timeout=2)

    assert not timer.is_alive()
    assert not manual.is_alive()
    assert manual_results == [expected]
