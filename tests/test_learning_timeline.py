from __future__ import annotations

import queue
import time

from computer_server.learning.recorder import LearningRecorder, _ObservationJob
from computer_server.learning.session import (
    LearningObservation,
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


class _FakeUIA:
    def is_password_field_focused(self):
        return False

    def focused_element_context(self):
        return {"role": "edit", "title": "Document"}

    def enrich_click(self, x, y):
        return {"role": "button", "title": f"Button {int(x)},{int(y)}"}


def _recorder_with_session() -> LearningRecorder:
    recorder = LearningRecorder()
    recorder._session = LearningSession(task_description=None, start_time=time.time())
    return recorder


def test_append_assigns_ids_and_queues_observation_job():
    recorder = _recorder_with_session()
    recorder._observation_queue = queue.Queue(maxsize=4)
    action = ObservedAction(
        timestamp=10,
        action_type=ObservedActionType.CLICK,
        app_name="Notepad",
        app_bundle_id="notepad.exe",
        window_title="Untitled - Notepad",
        x=10,
        y=20,
    )

    assert recorder._append(action, observation_trigger="click") is True

    assert action.action_id
    assert action.correlation_id
    assert recorder._session is not None
    assert recorder._session.actions == [action]
    assert recorder._session.metrics["action_count"] == 1
    assert recorder._session.metrics["pending_observations"] == 1
    job = recorder._observation_queue.get_nowait()
    assert job.action_id == action.action_id
    assert job.correlation_id == action.correlation_id
    assert job.trigger == "click"


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
