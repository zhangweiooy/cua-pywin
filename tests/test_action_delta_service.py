from __future__ import annotations

from computer_server.services.action_delta_service import build_post_action_delta


def test_post_action_delta_summarizes_node_changes() -> None:
    before = {
        "success": True,
        "pid": 10,
        "window_id": 20,
        "observation_id": "obs-before",
        "screenshot_id": "shot-before",
        "node_count": 2,
        "returned_node_count": 2,
        "tree_quality": "rich",
        "nodes": [
            {
                "source": "uia",
                "element_index": 1,
                "control_type": "Button",
                "name": "Send",
                "bbox": [1, 2, 3, 4],
            },
            {
                "source": "vision",
                "control_type": "Text",
                "content": "old",
                "bbox": [10, 10, 20, 20],
            },
        ],
    }
    after = {
        "success": True,
        "pid": 10,
        "window_id": 20,
        "observation_id": "obs-after",
        "screenshot_id": "shot-after",
        "node_count": 3,
        "returned_node_count": 3,
        "tree_quality": "rich",
        "nodes": [
            {
                "source": "uia",
                "element_index": 1,
                "control_type": "Button",
                "name": "Sent",
                "bbox": [1, 2, 3, 4],
            },
            {
                "source": "vision",
                "control_type": "Text",
                "content": "new",
                "bbox": [10, 10, 20, 20],
            },
            {
                "source": "uia",
                "element_index": 9,
                "control_type": "MenuItem",
                "name": "More",
                "bbox": [30, 30, 40, 40],
            },
        ],
    }

    delta = build_post_action_delta(before, after)

    assert delta["success"] is True
    assert delta["before_observation_id"] == "obs-before"
    assert delta["after_observation_id"] == "obs-after"
    assert delta["window_changed"] is False
    assert delta["node_count_delta"] == 1
    assert delta["added_node_count"] == 2
    assert delta["removed_node_count"] == 1
    assert delta["changed_text_count"] == 1
    assert delta["changed_text"][0]["before"]["text"] == "Send"
    assert delta["changed_text"][0]["after"]["text"] == "Sent"


def test_post_action_delta_reports_capture_errors() -> None:
    delta = build_post_action_delta(
        {"success": False, "error": "before failed"},
        {"success": True, "nodes": []},
    )

    assert delta == {
        "success": False,
        "reason": "capture_unavailable",
        "before_error": "before failed",
        "after_error": None,
    }
