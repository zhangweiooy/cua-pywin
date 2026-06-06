from __future__ import annotations

import json

from computer_server.recording.session import RECORDABLE_TOOLS, RecordedTurnContext, RecordingSession

EXPECTED_RECORDABLE_TOOLS = {
    "computer_click",
    "computer_perform_secondary_action",
    "computer_mouse",
    "computer_drag",
    "computer_type",
    "computer_press_key",
    "computer_set_value",
    "computer_page",
    "computer_open_url",
    "computer_scroll",
    "computer_launch_app",
    "computer_manage_window",
}


def test_write_turn_persists_element_ref(tmp_path):
    session = RecordingSession(str(tmp_path))
    turn_dir = tmp_path / "turn-00001"

    session._write_turn(
        str(turn_dir),
        "computer_click",
        {"element_index": 1, "pid": 10, "window_id": 20},
        {"success": True},
        screenshot_bytes=None,
        click_point=None,
        window_ref={"window_id": 20, "origin_x": 100, "origin_y": 200},
        element_ref={
            "recorded_element_index": 1,
            "control_type": "Button",
            "name": "保存",
        },
        turn_number=1,
    )

    data = json.loads((turn_dir / "action.json").read_text(encoding="utf-8"))

    assert data["element_ref"] == {
        "recorded_element_index": 1,
        "control_type": "Button",
        "name": "保存",
    }


def test_recordable_tools_are_current_action_tools():
    assert RECORDABLE_TOOLS == EXPECTED_RECORDABLE_TOOLS


def test_recorded_turn_context_groups_hook_payload():
    turn = RecordedTurnContext(
        tool_name="computer_click",
        args={"x": 10, "y": 20},
        result={"success": True},
        click_point=(10, 20),
        window_ref={"window_id": 123},
        element_ref={"name": "保存"},
    )

    assert turn.tool_name == "computer_click"
    assert turn.args["x"] == 10
    assert turn.click_point == (10, 20)
    assert turn.window_ref == {"window_id": 123}
    assert turn.element_ref == {"name": "保存"}
