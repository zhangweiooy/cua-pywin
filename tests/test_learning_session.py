from __future__ import annotations

from computer_server.learning.session import ObservedAction, ObservedActionType


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
    assert "ocr_region" not in data
    assert "ocr_window_x" not in data
    assert "ocr_window_y" not in data
