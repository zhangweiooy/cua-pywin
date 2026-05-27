from __future__ import annotations

import json

from computer_server.recording.session import RECORDABLE_TOOLS, RecordingSession


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


def test_low_level_pointer_primitives_are_recordable():
    assert "computer_move" in RECORDABLE_TOOLS
    assert "computer_mouse_down" in RECORDABLE_TOOLS
    assert "computer_mouse_up" in RECORDABLE_TOOLS
