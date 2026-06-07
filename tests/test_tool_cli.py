from __future__ import annotations

import argparse
import io
import os
import sys

import pytest

from computer_server.tool_cli import (
    DaemonInfo,
    DoctorCheck,
    _DaemonHttpClient,
    _call_result_to_json,
    _collect_doctor_checks,
    _doctor_summary,
    _execute_batch,
    _is_lightweight_inprocess_call,
    _mcp_import_mode,
    _parse_batch_actions,
    _parse_tool_arguments,
    _rewrite_implicit_call,
    _tool_to_json,
    build_parser,
)
from computer_server.tool_rpc import BATCH_HEADER


def test_rewrite_implicit_call() -> None:
    assert _rewrite_implicit_call(["computer_get_screen_size"]) == [
        "call",
        "computer_get_screen_size",
    ]
    assert _rewrite_implicit_call(["list-tools"]) == ["list-tools"]
    assert _rewrite_implicit_call(["doctor"]) == ["doctor"]


def test_parse_tool_arguments_requires_object() -> None:
    assert _parse_tool_arguments(None) == {}
    assert _parse_tool_arguments('{"x": 1}') == {"x": 1}

    with pytest.raises(SystemExit, match="tool arguments must be a JSON object"):
        _parse_tool_arguments("[1, 2, 3]")


def test_parse_tool_arguments_accepts_utf8_stdin_and_file(tmp_path, monkeypatch) -> None:
    encoded = '{"query":"\u5fae\u4fe1"}'
    argument_file = tmp_path / "arguments.json"
    argument_file.write_text("\ufeff" + encoded, encoding="utf-8")

    monkeypatch.setattr(sys, "stdin", io.StringIO("\ufeff" + encoded))
    assert _parse_tool_arguments("-") == {"query": "微信"}
    assert _parse_tool_arguments(f"@{argument_file}") == {"query": "微信"}


def test_parse_batch_actions_accepts_inline_file_and_stdin(tmp_path, monkeypatch) -> None:
    actions = [{"tool": "computer_get_screen_size", "arguments": {}}]
    encoded = '[{"tool":"computer_get_screen_size","arguments":{}}]'
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(encoded, encoding="utf-8")

    assert _parse_batch_actions(encoded) == actions
    assert _parse_batch_actions(str(batch_file)) == actions

    monkeypatch.setattr(sys, "stdin", io.StringIO("\ufeff" + encoded))
    assert _parse_batch_actions("-") == actions


def test_parse_batch_actions_merges_tool_defaults_shallowly() -> None:
    encoded = """
    {
      "defaults": {
        "computer_drag": {
          "window": {"id": 1, "pid": 2},
          "duration": 0.02,
          "coordinate_space": "window_resized"
        },
        "computer_press_key": {
          "window": {"id": 3, "pid": 4}
        }
      },
      "actions": [
        {
          "tool": "computer_drag",
          "arguments": {
            "start_x": 10,
            "start_y": 20,
            "end_x": 30,
            "end_y": 40,
            "duration": 0.05,
            "window": {"id": 9, "pid": 8}
          }
        },
        {"type": "wait", "milliseconds": 25},
        {
          "tool": "computer_press_key",
          "arguments": {"key": "Return"}
        }
      ]
    }
    """

    actions = _parse_batch_actions(encoded)

    assert actions == [
        {
            "tool": "computer_drag",
            "arguments": {
                "window": {"id": 9, "pid": 8},
                "duration": 0.05,
                "coordinate_space": "window_resized",
                "start_x": 10,
                "start_y": 20,
                "end_x": 30,
                "end_y": 40,
            },
        },
        {"type": "wait", "milliseconds": 25},
        {
            "tool": "computer_press_key",
            "arguments": {
                "window": {"id": 3, "pid": 4},
                "key": "Return",
            },
        },
    ]


def test_parse_batch_actions_rejects_invalid_defaults() -> None:
    with pytest.raises(
        SystemExit,
        match="batch defaults for 'computer_drag' must be an arguments object",
    ):
        _parse_batch_actions(
            '{"defaults":{"computer_drag":1},"actions":[]}'
        )

    with pytest.raises(
        SystemExit,
        match="batch object contains unsupported fields: variables",
    ):
        _parse_batch_actions(
            '{"defaults":{},"actions":[],"variables":{}}'
        )


def test_batch_parser_accepts_minimal_options() -> None:
    args = build_parser().parse_args(
        [
            "batch",
            "batch.json",
            "--continue-on-error",
            "--timeout",
            "10",
        ]
    )

    assert args.command == "batch"
    assert args.input == "batch.json"
    assert args.continue_on_error is True
    assert args.timeout == 10


def test_top_level_help_describes_batch_input_forms() -> None:
    help_text = " ".join(build_parser().format_help().split())

    assert "execute a JSON action array or {defaults, actions} object" in help_text


def test_call_help_describes_utf8_argument_sources() -> None:
    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    help_text = subparsers_action.choices["call"].format_help()

    assert "UTF-8 stdin" in help_text
    assert "'@path'" in help_text


def test_batch_help_is_self_contained() -> None:
    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    help_text = subparsers_action.choices["batch"].format_help()

    assert "per-tool defaults" in help_text
    assert "shallow-merged by tool name" in help_text
    assert "arguments override its defaults" in help_text
    assert "Wait entries never use defaults" in help_text
    assert "stops on the first failure" in help_text
    assert "suppress the agent cursor trajectory overlay" in help_text
    assert '"defaults"' in help_text
    assert '"actions"' in help_text
    assert "--timeout" in help_text
    assert "each tool HTTP request" in help_text


def test_public_subcommand_arguments_have_help_text() -> None:
    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    missing = []
    for command, subparser in subparsers_action.choices.items():
        for action in subparser._actions:
            if action.dest == "help" or action.help == argparse.SUPPRESS:
                continue
            if not isinstance(action.help, str) or not action.help.strip():
                missing.append(f"{command}:{action.dest}")

    assert missing == []


def test_execute_batch_mixes_waits_and_tool_calls(monkeypatch) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def request(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            return {
                "is_error": False,
                "structured_content": {"success": True, "value": 1},
            }

    client = Client()
    sleeps = []
    monkeypatch.setattr("computer_server.tool_cli.time.sleep", sleeps.append)

    result = _execute_batch(
        client,
        [
            {"type": "wait", "milliseconds": 25},
            {"tool": "computer_get_screen_size", "arguments": {}},
        ],
    )

    assert result["success"] is True
    assert result["processed"] == 2
    assert result["failed"] == 0
    assert sleeps == [0.025]
    assert client.calls == [
        (
            "POST",
            "/call",
            {
                "name": "computer_get_screen_size",
                "arguments": {},
            },
        )
    ]


def test_execute_batch_stops_and_releases_inputs_after_failure() -> None:
    class Client:
        def __init__(self) -> None:
            self.tools = []

        def request(self, method, path, payload=None):
            tool = payload["name"]
            self.tools.append(tool)
            if tool == "computer_click":
                return {
                    "is_error": False,
                    "structured_content": {"success": False, "error": "failed"},
                }
            return {
                "is_error": False,
                "structured_content": {"success": True},
            }

    client = Client()
    result = _execute_batch(
        client,
        [
            {"tool": "computer_click", "arguments": {"x": 1, "y": 2}},
            {"tool": "computer_press_key", "arguments": {"key": "Return"}},
        ],
    )

    assert result["success"] is False
    assert result["completed"] is False
    assert result["processed"] == 1
    assert result["failed"] == 1
    assert result["released_inputs"] is True
    assert client.tools == [
        "computer_click",
        "computer_release_all_inputs",
    ]


def test_batch_http_client_marks_requests(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def read(self):
            return b"{}"

    class Connection:
        def __init__(self, host, port, timeout):
            captured["connection"] = (host, port, timeout)

        def request(self, method, path, body=None, headers=None):
            captured["request"] = (method, path, body, headers)

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(
        "computer_server.tool_cli.http.client.HTTPConnection",
        Connection,
    )
    client = _DaemonHttpClient(
        DaemonInfo(host="127.0.0.1", port=1234, token="secret"),
        batch_mode=True,
    )

    assert client.request("POST", "/call", {"name": "example"}) == {}
    assert captured["request"][3][BATCH_HEADER] == "1"


def test_lightweight_inprocess_calls_are_explicit() -> None:
    assert _is_lightweight_inprocess_call("computer_get_screen_size", {})
    assert _is_lightweight_inprocess_call("computer_recording", {"action": "status"})
    assert _is_lightweight_inprocess_call("computer_learn", {"action": "status"})
    assert not _is_lightweight_inprocess_call("computer_recording", {"action": "start"})
    assert not _is_lightweight_inprocess_call("computer_get_window_state", {})


def test_mcp_import_mode_restores_environment(monkeypatch) -> None:
    monkeypatch.delenv("CUA_LIGHTWEIGHT_MCP_IMPORT", raising=False)
    monkeypatch.setenv("CUA_SKIP_SOM_WARMUP", "existing")

    with _mcp_import_mode(lightweight=True):
        assert os.environ["CUA_LIGHTWEIGHT_MCP_IMPORT"] == "1"
        assert os.environ["CUA_SKIP_SOM_WARMUP"] == "1"

    assert "CUA_LIGHTWEIGHT_MCP_IMPORT" not in os.environ
    assert os.environ["CUA_SKIP_SOM_WARMUP"] == "existing"


def test_call_result_to_json() -> None:
    class Result:
        is_error = False
        structured_content = {"success": True}
        content = []

    assert _call_result_to_json(Result()) == {
        "is_error": False,
        "structured_content": {"success": True},
        "content": [],
    }


def test_tool_to_json_exposes_stable_schema_fields() -> None:
    class Tool:
        name = "computer_example"
        description = "Example tool"
        parameters = {"type": "object"}
        output_schema = None
        annotations = None
        fn = lambda: None

    assert _tool_to_json(Tool()) == {
        "name": "computer_example",
        "description": "Example tool",
        "inputSchema": {"type": "object"},
    }


def test_serve_accepts_http_server_options() -> None:
    args = build_parser().parse_args(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8443",
            "--log-level",
            "debug",
            "--ssl-keyfile",
            "key.pem",
            "--ssl-certfile",
            "cert.pem",
            "--i-know-this-exposes-my-computer",
            "--no-cli-rpc",
        ]
    )

    assert args.host == "0.0.0.0"
    assert args.port == 8443
    assert args.log_level == "debug"
    assert args.ssl_keyfile == "key.pem"
    assert args.ssl_certfile == "cert.pem"
    assert args.i_know_this_exposes_my_computer is True
    assert args.no_cli_rpc is True


def test_doctor_parser_accepts_options() -> None:
    args = build_parser().parse_args(
        [
            "doctor",
            "--json",
            "--timeout",
            "0.5",
            "--no-dependencies",
            "--no-daemon",
        ]
    )

    assert args.command == "doctor"
    assert args.json is True
    assert args.timeout == 0.5
    assert args.no_dependencies is True
    assert args.no_daemon is True


def test_doctor_summary_counts_statuses() -> None:
    assert _doctor_summary(
        [
            DoctorCheck("a", "ok", "ok"),
            DoctorCheck("b", "warn", "warn"),
            DoctorCheck("c", "fail", "fail"),
        ]
    ) == {"ok": 1, "warn": 1, "fail": 1}


def test_collect_doctor_checks_can_skip_slow_sections() -> None:
    payload = _collect_doctor_checks(dependencies=False, daemon=False)

    names = {check["name"] for check in payload["checks"]}
    assert {"python", "platform", "state_dir", "model:omniparser_yolo", "model:tinyclip"}.issubset(names)
    assert "daemon" not in names
    assert not any(name.startswith("dependency:") for name in names)
