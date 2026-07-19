from __future__ import annotations

import json

import pytest

from computer_server.recording.replay import replay_trajectory
from computer_server.recording.session import (
    RECORDABLE_TOOLS,
    RecordingDirectoryError,
    RecordedTurnContext,
    RecordingSession,
)

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
    assert data["protocol_version"] == "0.2"
    assert data["args"] == {
        "element_index": 1,
        "window": {"id": 20, "pid": 10},
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


@pytest.mark.asyncio
async def test_replay_rejects_pre_02_recording(tmp_path):
    (tmp_path / "recording.json").write_text(
        json.dumps(
            {
                "format": "cua-win-recording",
                "protocol_version": "0.1",
            }
        ),
        encoding="utf-8",
    )

    async def executor(*_args, **_kwargs):
        raise AssertionError("0.1 recording must not execute")

    result = await replay_trajectory(
        str(tmp_path),
        executor,
        delay_ms=0,
    )

    assert result["completed"] is False
    assert result["turns_replayed"] == 0
    assert "expected '0.2'" in result["errors"][0]["error"]


def test_start_rejects_nonempty_recording_directory_without_relabeling(tmp_path):
    original_manifest = {
        "format": "cua-win-recording",
        "protocol_version": "0.1",
    }
    manifest_path = tmp_path / "recording.json"
    manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    turn_dir = tmp_path / "turn-00001"
    turn_dir.mkdir()
    (turn_dir / "action.json").write_text(
        json.dumps({"tool": "computer_type", "args": {"text": "old"}}),
        encoding="utf-8",
    )

    session = RecordingSession(str(tmp_path), capture_screenshots=False)

    with pytest.raises(RecordingDirectoryError, match="must be empty"):
        session.start()

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == original_manifest
    assert session.active is False


@pytest.mark.asyncio
async def test_replay_rejects_turn_with_wrong_protocol_version(tmp_path):
    (tmp_path / "recording.json").write_text(
        json.dumps(
            {
                "format": "cua-win-recording",
                "protocol_version": "0.2",
            }
        ),
        encoding="utf-8",
    )
    turn_dir = tmp_path / "turn-00001"
    turn_dir.mkdir()
    (turn_dir / "action.json").write_text(
        json.dumps(
            {
                "protocol_version": "0.1",
                "tool": "computer_type",
                "args": {"text": "old"},
            }
        ),
        encoding="utf-8",
    )

    async def executor(*_args, **_kwargs):
        raise AssertionError("wrong-version turn must not execute")

    result = await replay_trajectory(str(tmp_path), executor, delay_ms=0)

    assert result["completed"] is False
    assert result["turns_replayed"] == 0
    assert result["turns_failed"] == 1
    assert "turn protocol_version '0.1'" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_replay_counts_missing_action_file_as_failure(tmp_path):
    (tmp_path / "recording.json").write_text(
        json.dumps(
            {
                "format": "cua-win-recording",
                "protocol_version": "0.2",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "turn-00001").mkdir()

    async def executor(*_args, **_kwargs):
        raise AssertionError("missing turn must not execute")

    result = await replay_trajectory(str(tmp_path), executor, delay_ms=0)

    assert result["completed"] is False
    assert result["turns_replayed"] == 0
    assert result["turns_failed"] == 1
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["error"] == "Missing action.json"
    assert result["errors"][0]["error"] == "Missing action.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_file", ["recording.json", "action.json"])
async def test_replay_rejects_non_object_json(tmp_path, malformed_file):
    (tmp_path / "recording.json").write_text(
        json.dumps(
            {
                "format": "cua-win-recording",
                "protocol_version": "0.2",
            }
        ),
        encoding="utf-8",
    )
    if malformed_file == "recording.json":
        (tmp_path / "recording.json").write_text("[]", encoding="utf-8")
    else:
        turn_dir = tmp_path / "turn-00001"
        turn_dir.mkdir()
        (turn_dir / "action.json").write_text("[]", encoding="utf-8")

    async def executor(*_args, **_kwargs):
        raise AssertionError("malformed recording must not execute")

    result = await replay_trajectory(str(tmp_path), executor, delay_ms=0)

    assert result["completed"] is False
    assert result["turns_replayed"] == 0
    assert result["code"] == "INVALID_ARGUMENT"
    assert "JSON object" in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_replay_partial_failure_reports_unknown_outcome(tmp_path):
    (tmp_path / "recording.json").write_text(
        json.dumps(
            {
                "format": "cua-win-recording",
                "protocol_version": "0.2",
            }
        ),
        encoding="utf-8",
    )
    for number in (1, 2):
        turn_dir = tmp_path / f"turn-{number:05d}"
        turn_dir.mkdir()
        (turn_dir / "action.json").write_text(
            json.dumps(
                {
                    "protocol_version": "0.2",
                    "tool": "computer_type",
                    "args": {"text": str(number)},
                }
            ),
            encoding="utf-8",
        )

    calls = 0

    async def executor(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return {"success": False, "code": "INVALID_ARGUMENT", "error": "failed"}
        return {"success": True}

    result = await replay_trajectory(str(tmp_path), executor, delay_ms=0)

    assert result["completed"] is False
    assert result["turns_replayed"] == 1
    assert result["turns_failed"] == 1
    assert result["code"] == "ACTION_OUTCOME_UNKNOWN"
