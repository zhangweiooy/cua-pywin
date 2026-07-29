from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["CUA_SKIP_SOM_WARMUP"] = "1"

from computer_server.mcp_server import create_mcp_server
from computer_server.services.protocol_service import (
    ACTION_OUTCOMES,
    PROTOCOL_VERSION,
    RETRY_SAFETY_VALUES,
    STABLE_ERROR_CODES,
    action_execution_scope,
    begin_action_context,
    canonical_window,
    is_side_effect_call,
    mark_action_dispatch_started,
    protocolize_tool_result,
    transport_payload,
)


PUBLIC_TOOLS = (
    "computer_screenshot",
    "computer_get_screen_size",
    "computer_get_cursor_position",
    "computer_click",
    "computer_perform_secondary_action",
    "computer_mouse",
    "computer_drag",
    "computer_scroll",
    "computer_type",
    "computer_press_key",
    "computer_release_all_inputs",
    "computer_set_value",
    "computer_open_url",
    "computer_launch_app",
    "computer_get_window",
    "computer_manage_window",
    "computer_list_apps",
    "computer_list_windows",
    "computer_get_desktop_environment",
    "computer_get_window_state",
    "computer_zoom",
    "computer_page",
    "computer_recording",
    "computer_replay_trajectory",
    "computer_learn",
)

ENVELOPE_KEYS = {
    "protocol_version",
    "success",
    "data",
    "artifacts",
    "warnings",
    "error",
    "action",
}


def _arguments_for(tool_name: str) -> dict:
    if tool_name in {"computer_recording", "computer_learn"}:
        return {"action": "status"}
    if tool_name == "computer_page":
        return {"action": "get_text"}
    return {}


@pytest.mark.parametrize("tool_name", PUBLIC_TOOLS)
def test_protocol_boundary_wraps_success_for_every_public_tool(tool_name: str):
    arguments = _arguments_for(tool_name)
    side_effect = is_side_effect_call(tool_name, arguments)
    result = protocolize_tool_result(
        {"success": True, "value": 1},
        tool_name=tool_name,
        arguments=arguments,
        action_id="act-test" if side_effect else None,
    )
    envelope = result.structured_content

    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["protocol_version"] == PROTOCOL_VERSION
    assert envelope["success"] is True
    assert envelope["error"] is None
    assert envelope["warnings"] == []
    assert envelope["artifacts"] == []
    if side_effect:
        assert envelope["action"] == {
            "action_id": "act-test",
            "outcome": "completed",
            "retry_safety": "not_needed",
        }
    else:
        assert envelope["action"] is None


@pytest.mark.parametrize(
    ("source_code", "expected_code"),
    [
        ("validation_error", "INVALID_ARGUMENT"),
        ("target_not_found", "TARGET_NOT_FOUND"),
        ("snapshot_stale", "STALE_OBSERVATION"),
        ("uia_unavailable", "PROVIDER_UNAVAILABLE"),
        ("screenshot_failed", "CAPTURE_FAILED"),
        ("post_action_verification_failed", "POSTCONDITION_FAILED"),
    ],
)
def test_protocol_boundary_normalizes_stable_error_codes(
    source_code: str,
    expected_code: str,
):
    result = protocolize_tool_result(
        {
            "success": False,
            "code": source_code,
            "error": "expected failure",
        },
        tool_name="computer_click",
        arguments={},
        action_id="act-test",
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retry_safety"] in RETRY_SAFETY_VALUES
    assert envelope["action"]["outcome"] in ACTION_OUTCOMES


def test_postcondition_failure_keeps_completed_action_outcome():
    result = protocolize_tool_result(
        {
            "success": False,
            "code": "POSTCONDITION_FAILED",
            "error": "verification failed",
        },
        tool_name="computer_click",
        arguments={},
        action_id="act-test",
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["action"] == {
        "action_id": "act-test",
        "outcome": "completed",
        "retry_safety": "reobserve",
    }


def test_unknown_action_outcome_requires_reobservation():
    result = protocolize_tool_result(
        {
            "success": False,
            "code": "ACTION_OUTCOME_UNKNOWN",
            "error": "dispatch result is unknown",
        },
        tool_name="computer_type",
        arguments={},
        action_id="act-test",
    )
    envelope = result.structured_content

    assert envelope["action"] == {
        "action_id": "act-test",
        "outcome": "unknown",
        "retry_safety": "reobserve",
    }


def test_uncoded_side_effect_failure_defaults_to_unknown():
    result = protocolize_tool_result(
        {
            "success": False,
            "error": "OS delivery failed after dispatch may have started",
        },
        tool_name="computer_type",
        arguments={},
        action_id="act-test",
    )
    envelope = result.structured_content

    assert envelope["error"]["code"] == "ACTION_OUTCOME_UNKNOWN"
    assert envelope["action"] == {
        "action_id": "act-test",
        "outcome": "unknown",
        "retry_safety": "reobserve",
    }


def test_action_context_rejects_uncoded_preflight_failure():
    action_context = begin_action_context("computer_set_value", {})

    with action_execution_scope(action_context):
        result = protocolize_tool_result(
            {
                "success": False,
                "error": "No cached element at index 1",
            },
            tool_name="computer_set_value",
            arguments={},
            action_context=action_context,
        )

    envelope = result.structured_content
    assert envelope["error"]["code"] == "ACTION_REJECTED"
    assert envelope["action"]["outcome"] == "rejected"
    assert envelope["action"]["retry_safety"] == "safe"


def test_action_context_marks_uncoded_post_dispatch_failure_unknown():
    action_context = begin_action_context("computer_type", {})

    with action_execution_scope(action_context):
        mark_action_dispatch_started()
        result = protocolize_tool_result(
            {
                "success": False,
                "error": "clipboard paste failed",
            },
            tool_name="computer_type",
            arguments={},
            action_context=action_context,
        )

    envelope = result.structured_content
    assert envelope["error"]["code"] == "ACTION_OUTCOME_UNKNOWN"
    assert envelope["action"]["outcome"] == "unknown"
    assert envelope["action"]["retry_safety"] == "reobserve"


def test_explicit_action_rejection_remains_safe_after_outer_dispatch_marker():
    action_context = begin_action_context("computer_type", {})

    with action_execution_scope(action_context):
        mark_action_dispatch_started()
        result = protocolize_tool_result(
            {
                "success": False,
                "code": "ACTION_REJECTED",
                "error": "preflight rejected the action",
            },
            tool_name="computer_type",
            arguments={},
            action_context=action_context,
        )
    envelope = result.structured_content

    assert envelope["error"]["code"] == "ACTION_REJECTED"
    assert envelope["action"] == {
        "action_id": action_context.action_id,
        "outcome": "rejected",
        "retry_safety": "safe",
    }


def test_optional_capture_failure_is_a_warning():
    result = protocolize_tool_result(
        {
            "success": True,
            "value": 1,
            "capture_after": {
                "success": False,
                "error": "capture failed",
            },
        },
        tool_name="computer_click",
        arguments={"capture_after": True},
        action_id="act-test",
    )
    envelope = result.structured_content

    assert envelope["success"] is True
    assert envelope["data"] == {"value": 1}
    assert envelope["warnings"][0]["code"] == "CAPTURE_FAILED"


def test_recording_stop_path_becomes_directory_artifact(tmp_path):
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()

    result = protocolize_tool_result(
        {
            "success": True,
            "turns": 2,
            "path": str(recording_dir),
        },
        tool_name="computer_recording",
        arguments={"action": "stop"},
        action_id="act-test",
    )
    envelope = result.structured_content

    assert envelope["data"] == {"turns": 2}
    assert envelope["artifacts"] == [
        {
            "id": envelope["artifacts"][0]["id"],
            "type": "directory",
            "role": "recording",
            "mime_type": "application/x-directory",
            "content_index": None,
            "width": None,
            "height": None,
            "byte_count": None,
            "path": str(recording_dir),
        }
    ]


def test_public_projection_removes_legacy_keys_recursively():
    result = protocolize_tool_result(
        {
            "success": True,
            "snapshot_id": "snap-old",
            "id": 10,
            "window_id": 10,
            "window": {
                "id": 10,
                "pid": 20,
                "window_id": 10,
                "title": "not canonical",
            },
            "nested": {
                "window_ref": {"window_id": 10},
                "debug_image_out": "debug.png",
            },
        },
        tool_name="computer_get_window_state",
        arguments={},
    )
    data = result.structured_content["data"]

    assert data["observation_id"] == "snap-old"
    assert data["window"] == {"id": 10, "pid": 20}
    assert data["nested"] == {}
    assert "id" not in data
    assert "window_id" not in data


@pytest.mark.parametrize(
    "value",
    [
        {"id": 0, "pid": 20},
        {"id": 10, "pid": 0},
        {"id": 10},
        {"id": True, "pid": 20},
        {"id": 10, "pid": True},
    ],
)
def test_canonical_window_rejects_unusable_targets(value):
    assert canonical_window(value) is None


def test_public_projection_omits_unusable_window_binding():
    result = protocolize_tool_result(
        {
            "success": True,
            "windows": [
                {
                    "id": 0,
                    "pid": 20,
                    "source": "uia_xaml",
                    "window": {"id": 0, "pid": 20},
                }
            ],
        },
        tool_name="computer_list_windows",
        arguments={"include_xaml": True},
    )

    record = result.structured_content["data"]["windows"][0]
    assert "window" not in record


def test_transport_wrapper_has_fixed_shape():
    payload = transport_payload(
        SimpleNamespace(
            content=[],
            structured_content={
                "protocol_version": "0.2",
                "success": True,
            },
            is_error=False,
        )
    )

    assert payload == {
        "protocol_version": "0.2",
        "result": {
            "protocol_version": "0.2",
            "success": True,
        },
        "content": [],
        "transport_error": None,
    }


@pytest.mark.asyncio
async def test_all_public_tools_use_protocol_boundary_and_reset_schema():
    mcp = create_mcp_server()
    assert mcp.version == "0.2.4"
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert tuple(sorted(by_name)) == tuple(sorted(PUBLIC_TOOLS))
    for tool in tools:
        assert hasattr(tool.fn, "__wrapped__")
        properties = tool.parameters.get("properties", {})
        assert "snapshot_id" not in properties
        assert "window_id" not in properties
        assert "windowId" not in properties
        assert "debug_image_out" not in properties
        assert "screenshot_out_file" not in properties
        assert "from_zoom" not in properties
        if tool.name != "computer_list_windows":
            assert "pid" not in properties

        window_schema = properties.get("window")
        if window_schema is not None:
            object_schema = next(
                item
                for item in window_schema["anyOf"]
                if item.get("type") == "object"
            )
            assert object_schema["additionalProperties"] is False
            assert set(object_schema["properties"]) == {"id", "pid"}
            assert set(object_schema["required"]) == {"id", "pid"}


@pytest.mark.asyncio
async def test_schema_validation_failure_is_a_normal_protocol_result():
    result = await create_mcp_server().call_tool(
        "computer_click",
        {
            "pid": 1,
            "window_id": 2,
            "x": 1,
            "y": 2,
        },
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert envelope["action"]["outcome"] == "rejected"
    assert "not supported by protocol 0.2" in envelope["error"]["message"]


@pytest.mark.asyncio
async def test_recording_start_rejects_nonempty_directory_in_protocol(
    tmp_path,
):
    marker = tmp_path / "existing.txt"
    marker.write_text("keep", encoding="utf-8")

    result = await create_mcp_server().call_tool(
        "computer_recording",
        {
            "action": "start",
            "output_dir": str(tmp_path),
            "capture_screenshots": False,
        },
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert envelope["action"]["outcome"] == "rejected"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "recording.json").exists() is False


@pytest.mark.asyncio
async def test_replay_missing_directory_is_rejected_before_dispatch(tmp_path):
    result = await create_mcp_server().call_tool(
        "computer_replay_trajectory",
        {"recording_dir": str(tmp_path / "missing")},
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert envelope["action"]["outcome"] == "rejected"
    assert envelope["action"]["retry_safety"] == "safe"


@pytest.mark.asyncio
async def test_learning_uia_preflight_failure_is_rejected(monkeypatch, tmp_path):
    import computer_server.learning.recorder as recorder_module
    from computer_server.learning.session import LearningError

    class UnavailableRecorder:
        def start(
            self,
            *,
            session_id,
            task_description=None,
            output_dir="",
        ):
            return LearningError.UIA_UNAVAILABLE

    monkeypatch.setattr(recorder_module, "LearningRecorder", UnavailableRecorder)

    result = await create_mcp_server().call_tool(
        "computer_learn",
        {
            "action": "start",
            "session_id": "strict-preflight-session",
            "output_dir": str(tmp_path),
        },
    )
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert envelope["action"]["outcome"] == "rejected"
    assert envelope["action"]["retry_safety"] == "reobserve"


@pytest.mark.asyncio
async def test_learning_lifecycle_runs_blocking_recorder_calls_off_event_loop(
    monkeypatch,
    tmp_path,
):
    import computer_server.learning.recorder as recorder_module
    import computer_server.mcp_server as mcp_server_module

    calls = []

    async def tracked_to_thread(function, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    class Recorder:
        def start(
            self,
            *,
            session_id,
            task_description=None,
            output_dir="",
        ):
            self.session_id = session_id
            return None

        def status_dict(self):
            return {
                "recording": True,
                "session_id": self.session_id,
            }

        def stop(self, session_id):
            assert session_id == self.session_id
            session = SimpleNamespace(
                session_id=self.session_id,
                cutoff_sequence=0,
                start_time=100.0,
                end_time=112.9,
                observations=[object()] * 5,
                metrics={"degraded_observations": 1},
            )
            return True, (session, [])

    monkeypatch.setattr(mcp_server_module.asyncio, "to_thread", tracked_to_thread)
    monkeypatch.setattr(recorder_module, "LearningRecorder", Recorder)

    server = create_mcp_server()
    session_id = "off-event-loop-session"
    started = await server.call_tool(
        "computer_learn",
            {
                "action": "start",
                "session_id": session_id,
                "output_dir": str(tmp_path),
            },
    )
    stopped = await server.call_tool(
        "computer_learn",
        {
            "action": "stop",
            "session_id": session_id,
        },
    )

    assert started.structured_content["success"] is True
    assert stopped.structured_content["data"] == {
        "sessionId": session_id,
        "cutoffSequence": 0,
        "actions": [],
        "summary": {
            "durationSeconds": 12,
            "actionCount": 0,
            "observationCount": 5,
            "degradedObservationCount": 1,
        },
    }
    assert calls == ["start", "stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "computer_click",
            {
                "window": {"id": 2, "pid": 1},
                "element_index": 1,
            },
        ),
        (
            "computer_click",
            {
                "window": {"id": 2, "pid": 1},
                "coordinate_space": "window_resized",
                "x": 1,
                "y": 2,
            },
        ),
        (
            "computer_zoom",
            {
                "window": {"id": 2, "pid": 1},
                "x1": 0,
                "y1": 0,
                "x2": 10,
                "y2": 10,
            },
        ),
        (
            "computer_click",
            {
                "window": {"id": 2, "pid": 1},
                "coordinate_space": "zoom",
                "x": 1,
                "y": 2,
            },
        ),
    ],
)
async def test_state_derived_actions_require_fresh_binding(
    tool_name: str,
    arguments: dict,
):
    result = await create_mcp_server().call_tool(tool_name, arguments)
    envelope = result.structured_content

    assert envelope["success"] is False
    assert envelope["error"]["code"] == "OBSERVATION_REQUIRED"


@pytest.mark.asyncio
async def test_new_observation_stales_existing_zoom(monkeypatch):
    import computer_server.mcp_server as mcp_module
    import computer_server.services.window_state_service as window_state_service
    from computer_server.handlers.zoom_registry import (
        ZoomContext,
        get_zoom_registry,
    )

    async def fake_get_window_state_snapshot(
        _automation_handler,
        _pid,
        _window_id,
        **_kwargs,
    ):
        return {
            "success": True,
            "node_count": 0,
            "unfiltered_node_count": 0,
            "nodes": [],
            "tree_markdown": "",
            "screen_width": 10,
            "screen_height": 10,
            "capture_metadata": {"capture_backend": "wgc"},
            "_capture_geometry": {
                "compression_ratio": 1.0,
                "original_width": 10,
                "original_height": 10,
                "window_origin_x": 0,
                "window_origin_y": 0,
                "crop_origin_screen_x": 0,
                "crop_origin_screen_y": 0,
            },
        }

    monkeypatch.setattr(
        window_state_service,
        "get_window_state_snapshot",
        fake_get_window_state_snapshot,
    )
    monkeypatch.setattr(
        mcp_module,
        "_handlers",
        SimpleNamespace(automation=object()),
    )
    registry = get_zoom_registry()
    registry.clear_all()
    try:
        mcp = create_mcp_server()
        first = await mcp.call_tool(
            "computer_get_window_state",
            {"window": {"id": 2, "pid": 1}},
        )
        first_screenshot_id = first.structured_content["data"]["screenshot_id"]
        registry.set(
            1,
            2,
            ZoomContext(
                zoom_id="zoom-old",
                pid=1,
                window_id=2,
                crop_rect=(0, 0, 10, 10),
                scale_x=1.0,
                scale_y=1.0,
                output_width=10,
                output_height=10,
                padding=0,
                screenshot_id=first_screenshot_id,
            ),
        )

        await mcp.call_tool(
            "computer_get_window_state",
            {"window": {"id": 2, "pid": 1}},
        )
        result = await mcp.call_tool(
            "computer_click",
            {
                "window": {"id": 2, "pid": 1},
                "coordinate_space": "zoom",
                "zoom_id": "zoom-old",
                "x": 1,
                "y": 1,
            },
        )

        envelope = result.structured_content
        assert envelope["success"] is False
        assert envelope["error"]["code"] == "STALE_OBSERVATION"
        assert envelope["action"]["outcome"] == "rejected"
    finally:
        registry.clear_all()


def test_required_protocol_enums_are_complete():
    assert STABLE_ERROR_CODES.issuperset(
        {
            "INVALID_ARGUMENT",
            "TARGET_REQUIRED",
            "TARGET_NOT_FOUND",
            "TARGET_MISMATCH",
            "OBSERVATION_REQUIRED",
            "OBSERVATION_NOT_FOUND",
            "STALE_OBSERVATION",
            "OBSERVATION_TARGET_MISMATCH",
            "PERMISSION_DENIED",
            "UNSUPPORTED_OPERATION",
            "PROVIDER_UNAVAILABLE",
            "CAPTURE_FAILED",
            "ACTION_REJECTED",
            "ACTION_OUTCOME_UNKNOWN",
            "POSTCONDITION_FAILED",
        }
    )
