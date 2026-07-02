from __future__ import annotations

from computer_server.learning.session import (
    ElementContext,
    LearningLink,
    LearningObservation,
    LearningSession,
    ObservedAction,
    ObservedActionType,
)


def test_observed_action_ocr_region_is_not_serialized():
    action = ObservedAction(
        timestamp=1,
        action_type=ObservedActionType.TYPE_TEXT,
        app_name="notepad.exe",
        text="abc",
        ocr_region=b"png-bytes",
        ocr_window_x=10,
        ocr_window_y=20,
    )

    data = action.to_dict()

    assert data["text"] == "abc"
    assert "action_id" not in data
    assert "ocr_region" not in data
    assert "ocr_window_x" not in data
    assert "ocr_window_y" not in data


def test_learning_session_serializes_timeline_additively():
    action = ObservedAction(
        timestamp=1,
        action_type=ObservedActionType.CLICK,
        app_name="notepad.exe",
        app_bundle_id="notepad.exe",
        window_title="Untitled - Notepad",
        action_id="act_1",
        correlation_id="corr_1",
        after_observation_id="obs_1",
        x=10,
        y=20,
    )
    observation = LearningObservation(
        observation_id="obs_1",
        correlation_id="corr_1",
        trigger="click",
        timestamp=2,
        app_name="notepad.exe",
        app_bundle_id="notepad.exe",
        window_title="Untitled - Notepad",
        element_context=ElementContext(role="button", title="Save"),
        state_text="Window: notepad.exe\nNodes:",
        semantic_nodes=[{"element_index": 0, "role": "button", "title": "Save"}],
        tree_quality="poor",
        capture_backend="none",
        target_profile={"family": "generic"},
        capture_reliability="not_captured",
        semantic_degraded_reason="lightweight_semantic_context",
        delivery_risk="visible_target_required",
        timing={"total_ms": 3},
    )
    session = LearningSession(
        task_description="demo",
        start_time=100.0,
        actions=[action],
        observations=[observation],
        links=[
            LearningLink(
                action_id="act_1",
                observation_id="obs_1",
                correlation_id="corr_1",
                trigger="click",
            )
        ],
        apps={"notepad.exe"},
        urls={"https://example.test"},
    )

    data = session.to_dict(duration_seconds=5)

    assert data["action_count"] == 1
    assert data["actions"][0]["action_type"] == "click"
    assert data["actions"][0]["action_id"] == "act_1"
    assert data["actions"][0]["after_observation_id"] == "obs_1"
    assert data["observations"][0]["observation_id"] == "obs_1"
    assert data["observations"][0]["semantic_nodes"][0]["title"] == "Save"
    assert data["observations"][0]["target_profile"] == {"family": "generic"}
    assert data["observations"][0]["capture_reliability"] == "not_captured"
    assert (
        data["observations"][0]["semantic_degraded_reason"]
        == "lightweight_semantic_context"
    )
    assert data["observations"][0]["delivery_risk"] == "visible_target_required"
    assert data["links"][0]["action_id"] == "act_1"
    assert data["metrics"]["action_count"] == 1
    assert data["metrics"]["observation_count"] == 1
    assert data["metrics"]["target_profile_counts"] == {}
