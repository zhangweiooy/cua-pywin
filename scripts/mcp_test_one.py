#!/usr/bin/env python3
"""
MCP single-tool tester — agent-friendly.

Starts cua-pywin MCP in a subprocess, sends one tool call,
prints the result, and exits with code 0 (pass) or 1 (fail).

Usage:
    uv run python scripts/mcp_test_one.py <tool_name> [json_args] [--json] [--timeout N]
    uv run python scripts/mcp_test_one.py --list              # list available tools

Examples:
    uv run python scripts/mcp_test_one.py computer_get_screen_size
    uv run python scripts/mcp_test_one.py computer_file_exists '{"path": "C:/"}'
    uv run python scripts/mcp_test_one.py computer_get_screen_size --json
    uv run python scripts/mcp_test_one.py computer_screenshot --timeout 120

Output:
    On success: prints result + "PASS|<summary>" to stdout, exits 0
    On failure: prints "FAIL|<reason>" to stdout, exits 1
    With --json: also prints machine-readable JSON before the PASS/FAIL line
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SERVER_START_TIMEOUT = 30   # seconds to wait for server to accept initialize
TOOL_CALL_TIMEOUT = 20      # seconds per tool call
SHUTDOWN_TIMEOUT = 3        # seconds to wait for graceful shutdown

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str) -> None:
    """Print a FAIL line to stdout (not stderr) and exit 1."""
    print(f"FAIL|{msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    """Print a PASS line to stdout."""
    print(f"PASS|{msg}")


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

_request_id = 0


def _start_server():
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        # Skip SOM warm-up so the server starts fast.
        # For tests that need SOM, warm-up adds ~30s to the first call;
        # skipping it keeps one-shot test invocations responsive.
        "CUA_SKIP_SOM_WARMUP": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "computer_server.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env,
    )
    # Wrap stdin/stdout as text; stderr stays binary (drain thread handles decode)
    proc.stdin = io.TextIOWrapper(proc.stdin, encoding="utf-8", write_through=True)
    proc.stdout = io.TextIOWrapper(proc.stdout, encoding="utf-8")

    # Drain stderr in a background thread so the pipe buffer never
    # fills up and blocks the server process.  On Windows, FastMCP's
    # banner includes box-drawing characters that aren't valid UTF-8,
    # so we read raw bytes and decode with error replacement.
    _stderr_lines: list[str] = []

    def _drain():
        for line in proc.stderr:
            _stderr_lines.append(
                line.decode("utf-8", errors="replace").rstrip("\n")
            )

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    # Attach for later inspection
    proc._stderr_lines = _stderr_lines  # type: ignore[attr-defined]
    proc._stderr_thread = t  # type: ignore[attr-defined]

    # Quick crash check
    time.sleep(0.3)
    if proc.poll() is not None:
        tail = "".join(_stderr_lines[-20:])
        die(f"server exited code {proc.returncode}: {tail}")

    return proc


def _stderr_tail(proc) -> str:
    return "".join(proc._stderr_lines[-20:])  # type: ignore[attr-defined]


def _send(proc, msg: dict) -> None:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _read_response(proc, expected_id: int, timeout: int) -> dict:
    """Read JSON-RPC messages from stdout until the expected id appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Use a shorter poll interval so we can check process liveness
        line = proc.stdout.readline()
        if not line:
            # EOF — check if process died
            if proc.poll() is not None:
                tail = _stderr_tail(proc)
                raise RuntimeError(
                    f"server exited code {proc.returncode}: {tail}"
                )
            raise RuntimeError("server stdout closed unexpectedly")

        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("id") == expected_id:
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(
                    f"JSON-RPC error {err.get('code')}: {err.get('message')}"
                )
            return msg

    tail = _stderr_tail(proc)
    raise TimeoutError(
        f"no response for request {expected_id} within {timeout}s\n"
        f"stderr tail:\n{tail}"
    )


def _initialize(proc) -> dict:
    global _request_id
    _request_id = 1
    _send(proc, {
        "jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-test-one", "version": "1.0"},
        },
    })
    result = _read_response(proc, 1, SERVER_START_TIMEOUT)

    _send(proc, {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    })
    return result


def _call(proc, method: str, params: dict, timeout: int) -> dict:
    global _request_id
    _request_id += 1
    rid = _request_id
    _send(proc, {
        "jsonrpc": "2.0", "id": rid,
        "method": method, "params": params,
    })
    return _read_response(proc, rid, timeout)


def _shutdown(proc) -> None:
    """Send shutdown + close stdin, then kill. Fire-and-forget, never blocks."""
    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "method": "shutdown",
            "params": {},
        })
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# List tools
# ---------------------------------------------------------------------------

def do_list(json_mode: bool) -> None:
    proc = _start_server()
    try:
        _initialize(proc)
        tools_resp = _call(proc, "tools/list", {}, timeout=SERVER_START_TIMEOUT)
        tool_list = tools_resp["result"].get("tools", [])

        if json_mode:
            print(json.dumps([t["name"] for t in tool_list], indent=2))
        else:
            for t in tool_list:
                desc = t.get("description", "").split("\n")[0][:80]
                print(f"  {t['name']:45s}  {desc}")
            print(f"\n{len(tool_list)} tools total")
        sys.exit(0)
    except Exception as e:
        die(f"list failed: {e}")
    finally:
        _shutdown(proc)


# ---------------------------------------------------------------------------
# Call one tool
# ---------------------------------------------------------------------------

def do_call(tool_name: str, tool_args: dict, json_mode: bool, user_timeout: int | None = None) -> None:
    tool_timeout = user_timeout if user_timeout is not None else TOOL_CALL_TIMEOUT
    proc = _start_server()

    try:
        init_resp = _initialize(proc)
        server_name = init_resp["result"]["serverInfo"]["name"]
        server_ver = init_resp["result"]["serverInfo"]["version"]

        resp = _call(proc, "tools/call", {
            "name": tool_name,
            "arguments": tool_args,
        }, timeout=tool_timeout)

        content = resp["result"]["content"]

        if json_mode:
            output = {
                "tool": tool_name,
                "server": f"{server_name} v{server_ver}",
                "content": content,
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            for item in content:
                if item["type"] == "text":
                    try:
                        parsed = json.loads(item["text"])
                        print(json.dumps(parsed, indent=2, ensure_ascii=False))
                    except (json.JSONDecodeError, TypeError):
                        print(item["text"])
                elif item["type"] == "image":
                    data_len = len(item.get("data", ""))
                    mime = item.get("mimeType", "?")
                    print(f"[image: {mime}, {data_len} base64 chars]")
                elif item["type"] == "resource":
                    print(f"[resource: {item.get('resource', {})}]")
                else:
                    print(json.dumps(item, indent=2, ensure_ascii=False))

        ok(f"{tool_name} on {server_name} v{server_ver}")
        time.sleep(0.6)
        sys.exit(0)

    except Exception as e:
        die(f"{tool_name}: {e}")
    finally:
        _shutdown(proc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    json_mode = False
    list_mode = False
    tool_name = None
    tool_args: dict = {}
    user_timeout: int | None = None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            json_mode = True
        elif a == "--list":
            list_mode = True
        elif a == "--help" or a == "-h":
            print(__doc__)
            sys.exit(0)
        elif a == "--timeout":
            i += 1
            if i >= len(args):
                die("--timeout requires a number (seconds)")
            try:
                user_timeout = int(args[i])
            except ValueError:
                die(f"--timeout must be an integer, got: {args[i]}")
        elif tool_name is None:
            tool_name = a
        elif not tool_args:
            try:
                tool_args = json.loads(a)
            except json.JSONDecodeError:
                die(f"invalid JSON args: {a}")
        else:
            die(f"unexpected argument: {a}")
        i += 1

    if list_mode:
        return do_list(json_mode)

    if tool_name is None:
        die("missing tool name (use --list to see available tools)")

    return do_call(tool_name, tool_args, json_mode, user_timeout)


if __name__ == "__main__":
    main()
