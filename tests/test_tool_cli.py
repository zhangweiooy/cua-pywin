from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time

import pytest

from computer_server.tool_cli import (
    DaemonInfo,
    DoctorCheck,
    _DaemonHttpClient,
    _batch_dry_run,
    _call_result_to_json,
    _collect_doctor_checks,
    _check_interactive_desktop,
    _check_screenshot_capture,
    _check_window_enumeration,
    _cmd_call,
    _daemon_start_args,
    _doctor_summary,
    _execute_batch,
    _format_mcp_config,
    _is_lightweight_inprocess_call,
    _mcp_import_mode,
    _mcp_client_config,
    _parse_batch_actions,
    _parse_tool_arguments,
    _rewrite_implicit_call,
    _tool_to_json,
    build_parser,
)
from computer_server.tool_rpc import BATCH_HEADER


class _BinaryStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


def _transport_result(
    *,
    success: bool,
    data: dict | None = None,
    message: str = "failed",
) -> dict:
    error = None
    if not success:
        error = {
            "code": "ACTION_REJECTED",
            "message": message,
            "suggestion": None,
            "retry_safety": "safe",
            "details": {},
        }
    return {
        "protocol_version": "0.2",
        "result": {
            "protocol_version": "0.2",
            "success": success,
            "data": data or {},
            "artifacts": [],
            "warnings": [],
            "error": error,
            "action": None,
        },
        "content": [],
        "transport_error": None,
    }


def test_rewrite_implicit_call() -> None:
    assert _rewrite_implicit_call(["computer_get_screen_size"]) == [
        "call",
        "computer_get_screen_size",
    ]
    assert _rewrite_implicit_call(["list-tools"]) == ["list-tools"]
    assert _rewrite_implicit_call(["mcp"]) == ["mcp"]
    assert _rewrite_implicit_call(["mcp-config"]) == ["mcp-config"]
    assert _rewrite_implicit_call(["doctor"]) == ["doctor"]


def test_frozen_cli_daemon_reuses_current_executable(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "cua-win.exe"
    exe.write_text("", encoding="utf-8")
    metadata_file = tmp_path / "cli-daemon.json"
    monkeypatch.setattr(sys, "argv", [str(exe)])
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    args = _daemon_start_args("127.0.0.1", 12345, "token", metadata_file)

    assert args[:2] == [str(exe), "serve"]
    assert "-m" not in args
    assert "computer_server.tool_cli" not in args


def test_frozen_runtime_disables_bytecode_writes(monkeypatch, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    previous = sys.dont_write_bytecode
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "cua-win.exe")])
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    try:
        sys.dont_write_bytecode = False
        tool_cli._bootstrap_frozen_runtime_paths()
        assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
        assert sys.dont_write_bytecode is True
    finally:
        sys.dont_write_bytecode = previous


def test_parse_tool_arguments_requires_object() -> None:
    assert _parse_tool_arguments(None) == {}
    assert _parse_tool_arguments('{"x": 1}') == {"x": 1}

    with pytest.raises(SystemExit, match="tool arguments must be a JSON object"):
        _parse_tool_arguments("[1, 2, 3]")


def test_parse_tool_arguments_accepts_narrow_lenient_inline_object() -> None:
    assert _parse_tool_arguments("{action:status}") == {"action": "status"}
    assert _parse_tool_arguments("{pid:1234,window_id:5678,active:true}") == {
        "pid": 1234,
        "window_id": 5678,
        "active": True,
    }
    assert _parse_tool_arguments("{query:'wechat'}") == {"query": "wechat"}


def test_parse_tool_arguments_rejects_complex_lenient_inline_object() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_tool_arguments("{action:status,window:{id:1}}")

    message = str(exc_info.value)
    assert "invalid JSON arguments" in message
    assert "PowerShell 5.1" in message
    assert "PowerShell 7+" in message
    assert "cua-win call computer_learn -" in message


def test_parse_tool_arguments_accepts_utf8_stdin_and_file(tmp_path, monkeypatch) -> None:
    encoded = '{"query":"\u5fae\u4fe1"}'
    argument_file = tmp_path / "arguments.json"
    argument_file.write_text("\ufeff" + encoded, encoding="utf-8")

    monkeypatch.setattr(sys, "stdin", io.StringIO("\ufeff" + encoded))
    assert _parse_tool_arguments("-") == {"query": "微信"}
    assert _parse_tool_arguments(f"@{argument_file}") == {"query": "微信"}


def test_parse_tool_arguments_prefers_raw_utf8_stdin(monkeypatch) -> None:
    encoded = "\ufeff" + '{"query":"微信"}'

    monkeypatch.setattr(sys, "stdin", _BinaryStdin(encoded.encode("utf-8")))

    assert _parse_tool_arguments("-") == {"query": "微信"}


def test_parse_tool_arguments_keeps_stdin_strict_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{action:status}"))

    with pytest.raises(SystemExit) as exc_info:
        _parse_tool_arguments("-")

    message = str(exc_info.value)
    assert "invalid JSON arguments" in message
    assert "UTF-8 JSON object" in message
    assert "PowerShell 5.1" not in message


def test_parse_batch_actions_accepts_inline_file_and_stdin(tmp_path, monkeypatch) -> None:
    actions = [{"tool": "computer_get_screen_size", "arguments": {}}]
    encoded = '[{"tool":"computer_get_screen_size","arguments":{}}]'
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(encoded, encoding="utf-8")

    assert _parse_batch_actions(encoded) == actions
    assert _parse_batch_actions(str(batch_file)) == actions

    monkeypatch.setattr(sys, "stdin", io.StringIO("\ufeff" + encoded))
    assert _parse_batch_actions("-") == actions


def test_parse_batch_actions_prefers_raw_utf8_stdin(monkeypatch) -> None:
    encoded = '\ufeff[{"tool":"computer_type","arguments":{"text":"微信"}}]'

    monkeypatch.setattr(sys, "stdin", _BinaryStdin(encoded.encode("utf-8")))

    assert _parse_batch_actions("-") == [
        {"tool": "computer_type", "arguments": {"text": "微信"}}
    ]


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
            "--dry-run",
            "--timeout",
            "10",
        ]
    )

    assert args.command == "batch"
    assert args.input == "batch.json"
    assert args.continue_on_error is True
    assert args.dry_run is True
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


def test_mcp_config_outputs_safe_client_shapes() -> None:
    assert _mcp_client_config(command="cua-win") == {
        "mcpServers": {
            "cua-win": {
                "command": "cua-win",
                "args": ["mcp"],
                "env": {"PYTHONIOENCODING": "utf-8"},
            }
        }
    }

    json_config = json.loads(_format_mcp_config("json"))
    assert json_config["mcpServers"]["cua-win"]["args"] == ["mcp"]

    claude_command = _format_mcp_config("claude")
    assert claude_command.startswith("claude mcp add-json --scope user cua-win ")
    assert "\\\"args\\\":[\\\"mcp\\\"]" in claude_command

    assert _format_mcp_config("codex") == "codex mcp add cua-win -- cua-win mcp"


def test_batch_help_is_self_contained() -> None:
    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    help_text = subparsers_action.choices["batch"].format_help()
    compact_help = " ".join(help_text.split())

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
    assert "--dry-run" in help_text
    assert "without starting the daemon" in compact_help


def test_batch_dry_run_validates_and_summarizes_actions() -> None:
    actions = _parse_batch_actions(
        """
        {
          "defaults": {
            "computer_click": {"window": {"id": 1, "pid": 2}}
          },
          "actions": [
            {"tool": "computer_click", "arguments": {"x": 1, "y": 2}},
            {"type": "wait", "milliseconds": 50},
            {"tool": "computer_press_key", "arguments": {"key": "Return"}}
          ]
        }
        """
    )

    result = _batch_dry_run(actions)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["total"] == 3
    assert result["tool_calls"] == 2
    assert result["waits"] == 1
    assert result["tools"] == {
        "computer_click": 1,
        "computer_press_key": 1,
    }
    assert result["actions"][0]["argument_keys"] == ["window", "x", "y"]


def test_batch_dry_run_reports_first_invalid_action() -> None:
    result = _batch_dry_run(
        [
            {"tool": "computer_get_screen_size", "arguments": {}},
            {"type": "wait", "milliseconds": -1},
        ]
    )

    assert result["success"] is False
    assert result["dry_run"] is True
    assert result["failed_index"] == 1
    assert result["validated"] == 1
    assert "wait milliseconds" in result["error"]


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
            return _transport_result(success=True, data={"value": 1})

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
                return _transport_result(success=False)
            return _transport_result(success=True)

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


def test_daemon_start_then_status_uses_verified_process_and_health(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=tmp_path / "cli-daemon.json",
    )
    health = {"status": "ok", "pid": 1234, "tool_count": 25}
    monkeypatch.setattr(tool_cli, "_start_daemon", lambda timeout: info)
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (True, None, {"pid": daemon_info.pid}),
    )
    monkeypatch.setattr(tool_cli, "_probe", lambda daemon_info, timeout=2.0: health)

    args = argparse.Namespace(timeout=1, json=False)

    assert tool_cli._cmd_start(args) == 0
    assert "running: http://127.0.0.1:4567 pid=1234 tools=25" in capsys.readouterr().out

    assert tool_cli._cmd_status(argparse.Namespace(json=False)) == 0
    assert "running: http://127.0.0.1:4567 pid=1234 tools=25" in capsys.readouterr().out


def test_daemon_start_lock_serializes_processes(monkeypatch, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    monkeypatch.setenv("CUA_WIN_STATE_DIR", str(tmp_path))
    code = (
        "from computer_server.tool_cli import _daemon_start_lock;"
        "import os;"
        "assert os.environ['CUA_WIN_STATE_DIR'];"
        "ctx=_daemon_start_lock(timeout=5);"
        "ctx.__enter__();"
        "print('acquired', flush=True);"
        "ctx.__exit__(None, None, None)"
    )
    child = None
    with tool_cli._daemon_start_lock(timeout=1):
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.getcwd(),
            env={**os.environ, "CUA_WIN_STATE_DIR": str(tmp_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        time.sleep(0.3)
        assert child.poll() is None

    assert child is not None
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_daemon_status_cleans_stale_state_after_killed_process(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    removed = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (False, "pid_not_found", {"pid": daemon_info.pid}),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)

    assert tool_cli._cmd_status(argparse.Namespace(json=False)) == 0

    assert capsys.readouterr().out.strip() == "stopped"
    assert removed == [metadata_file]


def test_daemon_status_json_reports_stale_reason(monkeypatch, capsys, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=tmp_path / "cli-daemon.json",
    )
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (False, "pid_not_found", {"pid": daemon_info.pid}),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", lambda path=None: None)

    assert tool_cli._cmd_status(argparse.Namespace(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stopped"
    assert payload["stale"] is True
    assert payload["stale_reason"] == "pid_not_found"


def test_daemon_status_preserves_state_for_verified_unhealthy_process(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    removed = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (True, None, {"pid": daemon_info.pid}),
    )
    monkeypatch.setattr(tool_cli, "_probe", lambda daemon_info, timeout=2.0: None)
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)

    assert tool_cli._cmd_status(argparse.Namespace(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "host": "127.0.0.1",
        "pid": 1234,
        "port": 4567,
        "reason": "health_unreachable",
        "stale": False,
        "status": "unhealthy",
    }
    assert removed == []


def test_daemon_status_preserves_state_when_process_is_unverifiable(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    removed = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (
            False,
            "process_cmdline_unreadable",
            {"pid": daemon_info.pid, "error": "access denied"},
        ),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)

    assert tool_cli._cmd_status(argparse.Namespace(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "host": "127.0.0.1",
        "pid": 1234,
        "port": 4567,
        "reason": "process_cmdline_unreadable",
        "stale": False,
        "status": "unverifiable",
    }
    assert removed == []


def test_daemon_start_recovers_from_stale_state(monkeypatch, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    old_metadata = tmp_path / "old-daemon.json"
    old = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="old",
        pid=1111,
        metadata_file=old_metadata,
    )
    new = DaemonInfo(
        host="127.0.0.1",
        port=5678,
        token="new",
        pid=2222,
        metadata_file=tmp_path / "new-daemon.json",
    )
    removed = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: old)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (False, "pid_not_found", {"pid": daemon_info.pid}),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)
    monkeypatch.setattr(tool_cli, "_start_daemon", lambda timeout: new)

    assert tool_cli._ensure_daemon(timeout=1) == new
    assert removed == [old_metadata]


def test_daemon_ensure_does_not_spawn_over_verified_unhealthy_process(
    monkeypatch,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=tmp_path / "cli-daemon.json",
    )
    inspection = tool_cli.DaemonInspection(
        info=info,
        process_ok=True,
        health=None,
        stale_reason="health_unreachable",
    )
    started = []
    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: inspection)
    monkeypatch.setattr(
        tool_cli,
        "_start_daemon",
        lambda timeout: started.append(timeout),
    )

    with pytest.raises(RuntimeError, match="alive but unhealthy"):
        tool_cli._ensure_daemon(timeout=1)

    assert started == []


def test_daemon_ensure_does_not_spawn_when_process_is_unverifiable(
    monkeypatch,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=tmp_path / "cli-daemon.json",
    )
    inspection = tool_cli.DaemonInspection(
        info=info,
        process_ok=False,
        health=None,
        stale_reason="process_cmdline_unreadable",
        process_verifiable=False,
    )
    started = []
    removed = []
    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: inspection)
    monkeypatch.setattr(
        tool_cli,
        "_start_daemon",
        lambda timeout: started.append(timeout),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)

    with pytest.raises(RuntimeError, match="could not be verified"):
        tool_cli._ensure_daemon(timeout=1)

    assert started == []
    assert removed == []


def test_daemon_explicit_start_replaces_verified_unhealthy_process(
    monkeypatch,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    old = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="old",
        pid=1111,
        metadata_file=metadata_file,
    )
    actual = DaemonInfo(
        host="127.0.0.1",
        port=5678,
        token="actual",
        pid=2222,
        metadata_file=metadata_file,
    )
    inspections = iter(
        [
            tool_cli.DaemonInspection(
                info=old,
                process_ok=True,
                health=None,
                stale_reason="health_unreachable",
            ),
            tool_cli.DaemonInspection(
                info=actual,
                process_ok=True,
                health={"status": "ok", "pid": 2222},
            ),
        ]
    )
    terminated = []
    removed = []

    class Proc:
        pid = 2222
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: next(inspections))
    monkeypatch.setattr(
        tool_cli,
        "_terminate_daemon_process",
        lambda daemon_info, timeout: terminated.append((daemon_info, timeout)) or True,
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)
    monkeypatch.setattr(tool_cli, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(tool_cli, "_metadata_path", lambda: metadata_file)
    monkeypatch.setattr(tool_cli, "_find_free_port", lambda host: 5678)
    monkeypatch.setattr(tool_cli.secrets, "token_urlsafe", lambda size: "actual")
    monkeypatch.setattr(
        tool_cli,
        "_daemon_start_args",
        lambda *args: ["python", "-m", "computer_server.tool_cli", "serve"],
    )
    monkeypatch.setattr(tool_cli.subprocess, "Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(
        tool_cli,
        "_probe",
        lambda info, timeout=2.0: {"status": "ok", "pid": 2222},
    )

    assert tool_cli._start_daemon(timeout=1) == actual
    assert terminated == [(old, 5)]
    assert removed == [metadata_file]


def test_daemon_start_returns_metadata_pid_when_launcher_pid_differs(
    monkeypatch,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    actual = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="actual",
        pid=2222,
        metadata_file=metadata_file,
    )
    inspections = iter(
        [
            None,
            tool_cli.DaemonInspection(
                info=actual,
                process_ok=True,
                health={"status": "ok", "pid": 2222},
            ),
        ]
    )

    class Proc:
        pid = 1111
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: next(inspections))
    monkeypatch.setattr(tool_cli, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(tool_cli, "_metadata_path", lambda: metadata_file)
    monkeypatch.setattr(tool_cli, "_find_free_port", lambda host: 4567)
    monkeypatch.setattr(tool_cli.secrets, "token_urlsafe", lambda size: "candidate")
    monkeypatch.setattr(tool_cli, "_daemon_start_args", lambda *args: ["python", "-m", "computer_server.tool_cli", "serve"])
    monkeypatch.setattr(tool_cli.subprocess, "Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(tool_cli, "_probe", lambda info, timeout=2.0: {"status": "ok", "pid": 2222})

    assert tool_cli._start_daemon(timeout=1) == actual


def test_daemon_stop_does_not_terminate_wrong_pid(monkeypatch, capsys, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    removed = []
    terminated = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (
            False,
            "process_command_mismatch",
            {"pid": daemon_info.pid},
        ),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)
    monkeypatch.setattr(tool_cli, "_terminate_daemon_process", lambda *args: terminated.append(args))

    assert tool_cli._cmd_stop(argparse.Namespace(timeout=1)) == 0

    assert capsys.readouterr().out.strip() == "stopped"
    assert removed == [metadata_file]
    assert terminated == []


def test_daemon_stop_terminates_verified_unhealthy_process(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    removed = []
    terminated = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (True, None, {"pid": daemon_info.pid}),
    )
    monkeypatch.setattr(tool_cli, "_probe", lambda daemon_info, timeout=2.0: None)
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)
    monkeypatch.setattr(
        tool_cli,
        "_terminate_daemon_process",
        lambda daemon_info, timeout: terminated.append((daemon_info, timeout)) or True,
    )

    assert tool_cli._cmd_stop(argparse.Namespace(timeout=0)) == 0

    assert capsys.readouterr().out.strip() == "stopped"
    assert removed == [metadata_file]
    assert terminated == [(info, 5)]


def test_daemon_stop_preserves_state_when_process_is_unverifiable(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    removed = []
    terminated = []
    monkeypatch.setattr(tool_cli, "_load_daemon_info", lambda: info)
    monkeypatch.setattr(
        tool_cli,
        "_daemon_process_matches",
        lambda daemon_info: (
            False,
            "process_cmdline_unreadable",
            {"pid": daemon_info.pid, "error": "access denied"},
        ),
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)
    monkeypatch.setattr(
        tool_cli,
        "_terminate_daemon_process",
        lambda *args: terminated.append(args),
    )

    with pytest.raises(SystemExit, match="could not be verified"):
        tool_cli._cmd_stop(argparse.Namespace(timeout=1))

    assert capsys.readouterr().out == ""
    assert removed == []
    assert terminated == []


def test_daemon_stop_does_not_send_shutdown_when_health_pid_mismatches(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    inspection = tool_cli.DaemonInspection(
        info=info,
        process_ok=True,
        health={"status": "ok", "pid": 9999},
        stale_reason="health_pid_mismatch",
    )
    http_calls = []
    terminated = []
    removed = []
    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: inspection)
    monkeypatch.setattr(
        tool_cli,
        "_http_json",
        lambda *args, **kwargs: http_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        tool_cli,
        "_terminate_daemon_process",
        lambda daemon_info, timeout: terminated.append((daemon_info, timeout)) or True,
    )
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)

    assert tool_cli._cmd_stop(argparse.Namespace(timeout=0)) == 0

    assert capsys.readouterr().out.strip() == "stopped"
    assert http_calls == []
    assert terminated == [(info, 5)]
    assert removed == [metadata_file]


def test_terminate_daemon_uses_single_verified_process_instance(
    monkeypatch,
    tmp_path,
) -> None:
    import psutil

    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    calls = []

    class Proc:
        pid = 1234

        def create_time(self):
            calls.append("create_time")
            return 123.0

        def is_running(self):
            calls.append("is_running")
            return True

        def exe(self):
            calls.append("exe")
            return r"C:\Python\python.exe"

        def cmdline(self):
            calls.append("cmdline")
            return [
                r"C:\Python\python.exe",
                "-m",
                "computer_server.tool_cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "4567",
                "--token",
                "secret",
                "--metadata-file",
                str(metadata_file),
            ]

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(("wait", timeout))

    proc = Proc()
    process_lookups = []
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: process_lookups.append(pid) or proc,
    )

    assert tool_cli._terminate_daemon_process(info, timeout=3) is True
    assert process_lookups == [1234]
    assert calls == [
        "create_time",
        "is_running",
        "exe",
        "cmdline",
        "terminate",
        ("wait", 3),
    ]


def test_terminate_daemon_timeout_does_not_succeed_when_recheck_is_unverifiable(
    monkeypatch,
    tmp_path,
) -> None:
    import psutil

    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    calls = []
    cmdline_calls = 0

    class Proc:
        pid = 1234

        def create_time(self):
            return 123.0

        def is_running(self):
            return True

        def exe(self):
            return r"C:\Python\python.exe"

        def cmdline(self):
            nonlocal cmdline_calls
            cmdline_calls += 1
            if cmdline_calls > 1:
                raise psutil.AccessDenied(pid=self.pid)
            return [
                r"C:\Python\python.exe",
                "-m",
                "computer_server.tool_cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "4567",
                "--token",
                "secret",
                "--metadata-file",
                str(metadata_file),
            ]

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(("wait", timeout))
            raise psutil.TimeoutExpired(timeout, pid=self.pid)

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(psutil, "Process", lambda pid: Proc())

    assert tool_cli._terminate_daemon_process(info, timeout=3) is False
    assert calls == ["terminate", ("wait", 3)]


def test_daemon_doctor_reports_stale_state_without_removing_it(
    monkeypatch,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="secret",
        pid=1234,
        metadata_file=metadata_file,
    )
    inspection = tool_cli.DaemonInspection(
        info=info,
        process_ok=False,
        health=None,
        stale_reason="pid_not_found",
    )
    removed = []
    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: inspection)
    monkeypatch.setattr(tool_cli, "_remove_metadata", removed.append)

    check = tool_cli._check_daemon(timeout=1)

    assert check.status == "warn"
    assert check.message == "daemon metadata is stale: pid_not_found"
    assert removed == []


def test_daemon_doctor_accepts_clean_stopped_state(monkeypatch, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    monkeypatch.setenv("CUA_WIN_STATE_DIR", str(tmp_path))

    check = tool_cli._check_daemon(timeout=1)

    assert check.status == "ok"
    assert check.message == "daemon not running; metadata not found"


@pytest.mark.parametrize(
    "metadata",
    [
        "{broken",
        '{"host": "127.0.0.1"}',
    ],
)
def test_daemon_doctor_warns_on_invalid_metadata(
    monkeypatch,
    tmp_path,
    metadata,
) -> None:
    import computer_server.tool_cli as tool_cli

    monkeypatch.setenv("CUA_WIN_STATE_DIR", str(tmp_path))
    metadata_file = tmp_path / "cli-daemon.json"
    metadata_file.write_text(metadata, encoding="utf-8")

    check = tool_cli._check_daemon(timeout=1)

    assert check.status == "warn"
    assert check.message == (
        "daemon metadata exists but could not be parsed; metadata was preserved"
    )
    assert check.details == {
        "metadata_file": str(metadata_file),
        "stale_reason": "metadata_invalid",
    }
    assert metadata_file.read_text(encoding="utf-8") == metadata


def test_daemon_doctor_redacts_token_from_process_details(
    monkeypatch,
    tmp_path,
) -> None:
    import computer_server.tool_cli as tool_cli

    metadata_file = tmp_path / "cli-daemon.json"
    info = DaemonInfo(
        host="127.0.0.1",
        port=4567,
        token="super-secret-token",
        pid=1234,
        metadata_file=metadata_file,
    )
    inspection = tool_cli.DaemonInspection(
        info=info,
        process_ok=True,
        health=None,
        stale_reason="health_unreachable",
        details={
            "pid": 1234,
            "executable": r"C:\Python\python.exe",
            "command_line": [
                r"C:\Python\python.exe",
                "-m",
                "computer_server.tool_cli",
                "serve",
                "--token",
                "super-secret-token",
            ],
        },
    )
    monkeypatch.setattr(tool_cli, "_load_and_inspect_daemon", lambda timeout=2.0: inspection)

    payload = tool_cli._check_daemon(timeout=1).to_json()
    serialized = json.dumps(payload)

    assert "super-secret-token" not in serialized
    assert "<redacted>" in serialized


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
        "protocol_version": "0.2",
        "result": {"success": True},
        "content": [],
        "transport_error": None,
    }


def test_call_output_preserves_failure_envelope(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    async def fake_inprocess_call(_tool_name, _tool_args):
        return _transport_result(
            success=False,
            message="WGC and fallback failed",
        )

    monkeypatch.setattr(
        "computer_server.tool_cli._inprocess_call",
        fake_inprocess_call,
    )
    output_path = tmp_path / "capture.png"
    args = argparse.Namespace(
        tool="computer_screenshot",
        arguments="{}",
        no_daemon=True,
        start_timeout=30.0,
        timeout=30.0,
        output=str(output_path),
        output_dir=None,
        json=False,
    )

    exit_code = _cmd_call(args)
    printed = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert printed["success"] is False
    assert printed["error"]["message"] == "WGC and fallback failed"
    assert output_path.exists() is False


def test_call_output_saves_failure_artifact_before_returning_failure(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    image_bytes = b"captured-after-action"
    payload = _transport_result(
        success=False,
        message="postcondition failed",
    )
    payload["result"]["error"]["code"] = "POSTCONDITION_FAILED"
    payload["result"]["error"]["retry_safety"] = "reobserve"
    payload["result"]["artifacts"] = [
        {
            "id": "capture-after",
            "type": "image",
            "role": "capture_after",
            "mime_type": "image/png",
            "content_index": 0,
        }
    ]
    payload["content"] = [
        {
            "type": "image",
            "mimeType": "image/png",
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }
    ]

    async def fake_inprocess_call(_tool_name, _tool_args):
        return payload

    monkeypatch.setattr(
        "computer_server.tool_cli._inprocess_call",
        fake_inprocess_call,
    )
    output_path = tmp_path / "capture.png"
    args = argparse.Namespace(
        tool="computer_click",
        arguments="{}",
        no_daemon=True,
        start_timeout=30.0,
        timeout=30.0,
        output=str(output_path),
        output_dir=None,
        json=False,
    )

    exit_code = _cmd_call(args)
    printed = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert printed["success"] is False
    assert output_path.read_bytes() == image_bytes


def test_call_output_prints_result_when_local_artifact_write_fails(
    monkeypatch,
    capsys,
) -> None:
    async def fake_inprocess_call(_tool_name, _tool_args):
        return _transport_result(success=True)

    def fail_save(*_args, **_kwargs):
        raise SystemExit("disk is read-only")

    monkeypatch.setattr(
        "computer_server.tool_cli._inprocess_call",
        fake_inprocess_call,
    )
    monkeypatch.setattr(
        "computer_server.tool_cli._save_call_images",
        fail_save,
    )
    args = argparse.Namespace(
        tool="computer_screenshot",
        arguments="{}",
        no_daemon=True,
        start_timeout=30.0,
        timeout=30.0,
        output="capture.png",
        output_dir=None,
        json=False,
    )

    exit_code = _cmd_call(args)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(captured.out)["success"] is True
    assert "artifact save failed: disk is read-only" in captured.err


def test_tool_to_json_exposes_stable_schema_fields() -> None:
    class Tool:
        name = "computer_example"
        description = "Example tool"
        parameters = {"type": "object"}
        output_schema = None
        annotations = None

        @staticmethod
        def fn():
            return None

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
            "--no-desktop",
            "--no-daemon",
        ]
    )

    assert args.command == "doctor"
    assert args.json is True
    assert args.timeout == 0.5
    assert args.no_dependencies is True
    assert args.no_desktop is True
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
    payload = _collect_doctor_checks(dependencies=False, desktop=False, daemon=False)

    names = {check["name"] for check in payload["checks"]}
    assert {
        "python",
        "platform",
        "state_dir",
        "model:omniparser_yolo",
        "model:omniparser_florence2",
        "model:rapidocr_ppocrv4",
    }.issubset(names)
    assert "daemon" not in names
    assert not any(name.startswith("desktop:") for name in names)
    assert not any(name.startswith("dependency:") for name in names)


def test_collect_doctor_checks_includes_desktop_section(monkeypatch) -> None:
    import computer_server.tool_cli as tool_cli

    monkeypatch.setattr(
        tool_cli,
        "_check_interactive_desktop",
        lambda: DoctorCheck("desktop:interactive", "ok", "interactive"),
    )
    monkeypatch.setattr(
        tool_cli,
        "_check_window_enumeration",
        lambda: DoctorCheck("desktop:windows", "ok", "windows"),
    )
    monkeypatch.setattr(
        tool_cli,
        "_check_screenshot_capture",
        lambda: DoctorCheck("desktop:screenshot", "ok", "screenshot"),
    )

    payload = _collect_doctor_checks(dependencies=False, daemon=False)

    names = {check["name"] for check in payload["checks"]}
    assert {
        "desktop:interactive",
        "desktop:windows",
        "desktop:screenshot",
    }.issubset(names)


def test_desktop_doctor_checks_fail_on_non_windows(monkeypatch) -> None:
    import computer_server.tool_cli as tool_cli

    monkeypatch.setattr(tool_cli.sys, "platform", "linux")

    assert _check_interactive_desktop().status == "fail"
    assert _check_window_enumeration().status == "fail"
    assert _check_screenshot_capture().status == "fail"


def test_florence_doctor_rejects_git_lfs_pointer(monkeypatch, tmp_path) -> None:
    import computer_server.tool_cli as tool_cli

    model_dir = tmp_path / "icon_caption"
    model_dir.mkdir()
    for name in tool_cli.FLORENCE2_REQUIRED_FILES:
        path = model_dir / name
        if name == "model.safetensors":
            path.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:deadbeef\n"
                "size 1083916964\n",
                encoding="ascii",
            )
        else:
            path.write_text("test", encoding="ascii")
    monkeypatch.setattr(tool_cli, "FLORENCE2_MODEL_DIR", model_dir)

    check = tool_cli._check_florence2_bundle()

    assert check.status == "fail"
    assert "git lfs pull" in check.message
    assert check.details["git_lfs_pointer"] is True


def test_yolo_doctor_verifies_manifest_hash(monkeypatch, tmp_path) -> None:
    import hashlib
    import json

    import computer_server.tool_cli as tool_cli

    model_dir = tmp_path / "icon_detect"
    model_dir.mkdir()
    content = b"bundled-yolo"
    (model_dir / "model.pt").write_bytes(content)
    (model_dir / "model_manifest.json").write_text(
        json.dumps(
            {
                "source": {
                    "repository": "microsoft/OmniParser-v2.0",
                    "revision": "test-revision",
                },
                "sha256": {
                    "model.pt": hashlib.sha256(content).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_cli, "OMNIPARSER_YOLO_DIR", model_dir)

    check = tool_cli._check_omniparser_weight()

    assert check.status == "ok"
    assert check.details["revision"] == "test-revision"


def test_yolo_doctor_rejects_checksum_mismatch(monkeypatch, tmp_path) -> None:
    import json

    import computer_server.tool_cli as tool_cli

    model_dir = tmp_path / "icon_detect"
    model_dir.mkdir()
    (model_dir / "model.pt").write_bytes(b"unexpected")
    (model_dir / "model_manifest.json").write_text(
        json.dumps(
            {
                "source": {
                    "repository": "microsoft/OmniParser-v2.0",
                    "revision": "test-revision",
                },
                "sha256": {"model.pt": "0" * 64},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_cli, "OMNIPARSER_YOLO_DIR", model_dir)

    check = tool_cli._check_omniparser_weight()

    assert check.status == "fail"
    assert "checksum mismatch" in check.message


def test_rapidocr_doctor_verifies_bundle(monkeypatch, tmp_path) -> None:
    import hashlib
    import json

    import computer_server.tool_cli as tool_cli

    model_dir = tmp_path / "rapidocr_ppocrv4"
    model_dir.mkdir()
    hashes = {}
    for name in tool_cli.RAPIDOCR_REQUIRED_FILES:
        if name == "model_manifest.json":
            continue
        content = f"test-{name}".encode()
        (model_dir / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (model_dir / "model_manifest.json").write_text(
        json.dumps(
            {
                "source": {"distribution": "rapidocr", "version": "3.8.1"},
                "sha256": hashes,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_cli, "RAPIDOCR_MODEL_DIR", model_dir)

    check = tool_cli._check_rapidocr_bundle()

    assert check.status == "ok"
    assert check.details["source_version"] == "3.8.1"


def test_rapidocr_doctor_rejects_git_lfs_pointer(monkeypatch, tmp_path) -> None:
    import hashlib
    import json

    import computer_server.tool_cli as tool_cli

    model_dir = tmp_path / "rapidocr_ppocrv4"
    model_dir.mkdir()
    hashes = {}
    for name in tool_cli.RAPIDOCR_REQUIRED_FILES:
        if name == "model_manifest.json":
            continue
        content = (
            b"version https://git-lfs.github.com/spec/v1\n"
            if name.endswith(".onnx")
            else b"dictionary"
        )
        (model_dir / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    (model_dir / "model_manifest.json").write_text(
        json.dumps(
            {
                "source": {"distribution": "rapidocr", "version": "3.8.1"},
                "sha256": hashes,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_cli, "RAPIDOCR_MODEL_DIR", model_dir)

    check = tool_cli._check_rapidocr_bundle()

    assert check.status == "fail"
    assert "git lfs pull" in check.message
