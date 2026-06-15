"""Daemon-first command line client for local MCP tool calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import importlib.metadata
import importlib.util
import json
import logging
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .tool_rpc import (
    AUTH_HEADER,
    BATCH_HEADER,
    DAEMON_TOKEN_ENV,
    call_result_to_json as _call_result_to_json,
    tool_to_json as _tool_to_json,
)


def _bootstrap_frozen_runtime_paths() -> None:
    if not (getattr(sys, "frozen", False) or "__compiled__" in globals()):
        return
    script_path = Path(sys.argv[0])
    if script_path.suffix.lower() == ".exe":
        base_dir = script_path.resolve().parent
    else:
        base_dir = Path(sys.executable).resolve().parent
    for relative in ("win32", "win32/lib", "Pythonwin"):
        path = base_dir / relative
        if path.exists():
            text = str(path)
            if text not in sys.path:
                sys.path.append(text)
    pywin32_system32 = base_dir / "pywin32_system32"
    if pywin32_system32.exists():
        os.environ["PATH"] = (
            str(pywin32_system32) + os.pathsep + os.environ.get("PATH", "")
        )
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(pywin32_system32))
            except OSError:
                pass


_bootstrap_frozen_runtime_paths()


CLI_NAME = "cua-win"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_START_TIMEOUT = 60.0
DEFAULT_REQUEST_TIMEOUT = 120.0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
COMMANDS = {
    "serve",
    "start",
    "status",
    "stop",
    "doctor",
    "list-tools",
    "describe",
    "call",
    "batch",
}
LIGHTWEIGHT_INPROCESS_TOOLS = {
    "computer_get_screen_size",
    "computer_get_cursor_position",
    "computer_get_window",
    "computer_list_apps",
    "computer_list_windows",
    "computer_get_desktop_environment",
}

DEPENDENCY_CHECKS = (
    ("fastmcp", "fastmcp", "MCP server"),
    ("fastapi", "fastapi", "HTTP server"),
    ("uvicorn", "uvicorn", "HTTP runner"),
    ("PIL", "pillow", "image handling"),
    ("pynput", "pynput", "input injection"),
    ("win32gui", "pywin32", "Windows APIs"),
    ("comtypes", "comtypes", "COM / MSAA"),
    ("uiautomation", "uiautomation", "UI Automation"),
    ("mss", "mss", "screen capture"),
    ("psutil", "psutil", "process inspection"),
    ("cv2", "opencv-python-headless", "SOM image processing"),
    ("torch", "torch", "vision model runtime"),
    ("rapidocr", "rapidocr", "OCR"),
    ("onnxruntime", "onnxruntime", "OCR runtime"),
    ("ultralytics", "ultralytics", "OmniParser YOLO"),
    ("transformers", "transformers", "OmniParser Florence-2"),
    ("einops", "einops", "Florence-2 tensor operations"),
    ("timm", "timm", "Florence-2 vision encoder"),
    ("winrt.windows.graphics.capture", "winrt-windows-graphics-capture", "WGC window capture"),
    (
        "winrt.windows.graphics.capture.interop",
        "winrt-windows-graphics-capture-interop",
        "WGC HWND interop",
    ),
    (
        "winrt.windows.graphics.directx.direct3d11.interop",
        "winrt-windows-graphics-directx-direct3d11-interop",
        "WGC D3D11 interop",
    ),
)

FLORENCE2_REQUIRED_FILES = (
    "config.json",
    "configuration_florence2.py",
    "generation_config.json",
    "model_manifest.json",
    "model.safetensors",
    "modeling_florence2.py",
    "preprocessor_config.json",
    "processing_florence2.py",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)
FLORENCE2_MIN_MODEL_BYTES = 1_000_000_000
OMNIPARSER_YOLO_DIR = (
    Path(__file__).resolve().parents[1]
    / "som"
    / "weights"
    / "icon_detect"
)
FLORENCE2_MODEL_DIR = (
    Path(__file__).resolve().parent
    / "models"
    / "omniparser_v2"
    / "icon_caption"
)
RAPIDOCR_REQUIRED_FILES = (
    "ch_PP-OCRv4_det_mobile.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "ch_PP-OCRv4_rec_mobile.onnx",
    "ppocr_keys_v1.txt",
    "model_manifest.json",
)
RAPIDOCR_MODEL_DIR = (
    Path(__file__).resolve().parent
    / "models"
    / "rapidocr_ppocrv4"
)

MAX_BATCH_ACTIONS = 10_000
MAX_BATCH_WAIT_MS = 600_000


class _HelpFormatter(
    argparse.RawDescriptionHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter,
):
    """Preserve examples while showing defaults for documented options."""


@dataclass(frozen=True)
class DaemonInfo:
    host: str
    port: int
    token: str
    pid: int | None = None
    metadata_file: Path | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    details: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class _DaemonHttpClient:
    """Small persistent HTTP client used by multi-call CLI commands."""

    def __init__(
        self,
        info: DaemonInfo,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        batch_mode: bool = False,
    ) -> None:
        self._info = info
        self._timeout = timeout
        self._batch_mode = batch_mode
        self._connection = http.client.HTTPConnection(
            info.host,
            info.port,
            timeout=timeout,
        )

    def close(self) -> None:
        self._connection.close()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {AUTH_HEADER: self._info.token, "Accept": "application/json"}
        if self._batch_mode:
            headers[BATCH_HEADER] = "1"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            self._connection.request(method, path, body=body, headers=headers)
            response = self._connection.getresponse()
            raw = response.read().decode("utf-8")
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            raise RuntimeError(str(exc)) from exc

        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}: {raw}")
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON response: {raw[:200]}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("daemon returned a non-object JSON response")
        return decoded


def _state_dir() -> Path:
    configured = os.environ.get("CUA_WIN_STATE_DIR")
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "cua-pywin"
    return Path.home() / ".cua-pywin"


def _metadata_path() -> Path:
    return _state_dir() / "cli-daemon.json"


def _log_path() -> Path:
    return _state_dir() / "cli-daemon.log"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _load_daemon_info() -> DaemonInfo | None:
    path = _metadata_path()
    data = _read_json(path)
    if not data:
        return None
    try:
        return DaemonInfo(
            host=str(data["host"]),
            port=int(data["port"]),
            token=str(data["token"]),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            metadata_file=path,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _metadata_payload(info: DaemonInfo) -> dict[str, Any]:
    return {
        "host": info.host,
        "port": info.port,
        "token": info.token,
        "pid": info.pid,
        "started_at": time.time(),
        "client": CLI_NAME,
    }


def _remove_metadata(path: Path | None = None) -> None:
    target = path or _metadata_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def _find_free_port(host: str = DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _daemon_python_executable() -> str:
    script_path = Path(sys.argv[0])
    adjacent_python = script_path.parent / "python.exe"
    if script_path.suffix.lower() == ".exe" and adjacent_python.exists():
        return str(adjacent_python)
    return sys.executable


def _is_frozen_cli_executable() -> bool:
    script_path = Path(sys.argv[0])
    adjacent_python = script_path.parent / "python.exe"
    return (
        script_path.suffix.lower() == ".exe"
        and not adjacent_python.exists()
        and (getattr(sys, "frozen", False) or "__compiled__" in globals())
    )


def _daemon_start_args(
    host: str,
    port: int,
    token: str,
    metadata_file: Path,
) -> list[str]:
    serve_args = [
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--token",
        token,
        "--metadata-file",
        str(metadata_file),
        "--quiet",
    ]
    if _is_frozen_cli_executable():
        return [str(Path(sys.argv[0]).resolve()), *serve_args]
    return [
        _daemon_python_executable(),
        "-m",
        "computer_server.tool_cli",
        *serve_args,
    ]


def _http_json(
    info: DaemonInfo,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    data = None
    headers = {AUTH_HEADER: info.token, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        info.base_url + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(str(reason)) from exc
    except OSError as exc:
        raise RuntimeError(str(exc)) from exc

    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON response: {raw[:200]}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("daemon returned a non-object JSON response")
    return decoded


def _probe(info: DaemonInfo, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        return _http_json(info, "GET", "/status", timeout=timeout)
    except RuntimeError:
        return None


def _start_daemon(timeout: float = DEFAULT_START_TIMEOUT) -> DaemonInfo:
    existing = _load_daemon_info()
    if existing and _probe(existing):
        return existing
    if existing:
        _remove_metadata(existing.metadata_file)

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    metadata_file = _metadata_path()
    host = DEFAULT_HOST
    port = _find_free_port(host)
    token = secrets.token_urlsafe(32)
    info = DaemonInfo(host=host, port=port, token=token, metadata_file=metadata_file)

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
    }
    args = _daemon_start_args(host, port, token, metadata_file)

    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    log_file = _log_path().open("ab")
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(Path.cwd()),
            env=env,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    finally:
        log_file.close()

    info = DaemonInfo(
        host=host,
        port=port,
        token=token,
        pid=proc.pid,
        metadata_file=metadata_file,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _remove_metadata(metadata_file)
            raise RuntimeError(
                f"daemon exited with code {proc.returncode}; see {_log_path()}"
            )
        status = _probe(info, timeout=1.0)
        if status:
            return info
        time.sleep(0.2)

    if proc.poll() is not None:
        _remove_metadata(metadata_file)
        raise RuntimeError(
            f"daemon exited with code {proc.returncode}; see {_log_path()}"
        )
    try:
        proc.kill()
        proc.wait(timeout=5)
    except OSError:
        pass
    except subprocess.TimeoutExpired:
        pass
    _remove_metadata(metadata_file)
    raise TimeoutError(f"daemon did not become ready within {timeout:.0f}s; see {_log_path()}")


def _ensure_daemon(timeout: float = DEFAULT_START_TIMEOUT) -> DaemonInfo:
    info = _load_daemon_info()
    if info and _probe(info):
        return info
    if info:
        _remove_metadata(info.metadata_file)
    return _start_daemon(timeout=timeout)


def _read_stdin_utf8() -> str:
    """Read stdin as UTF-8 when raw bytes are available.

    ``cua-win`` documents stdin JSON as UTF-8.  Using ``sys.stdin.read()``
    routes through Python's text wrapper and can inherit a legacy Windows
    console encoding in subprocess contexts.  Prefer the raw buffer so pipes
    and redirected files keep Chinese text intact; StringIO tests still use the
    text fallback.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SystemExit(f"stdin must be UTF-8 JSON: {exc}") from exc
    return sys.stdin.read().lstrip("\ufeff")


def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if raw is None or not raw.strip():
        return {}
    if raw == "-":
        source = _read_stdin_utf8()
    elif raw.startswith("@"):
        path_text = raw[1:]
        if not path_text:
            raise SystemExit("argument file path is required after @")
        try:
            source = Path(path_text).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"could not read argument file {path_text!r}: {exc}") from exc
    else:
        source = raw
    source = source.lstrip("\ufeff")
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON arguments: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("tool arguments must be a JSON object")
    return parsed


def _parse_batch_actions(raw: str | None) -> list[dict[str, Any]]:
    if raw is None or raw == "-":
        source = _read_stdin_utf8()
    else:
        stripped = raw.lstrip()
        if stripped.startswith(("[", "{")):
            source = raw
        else:
            try:
                source = Path(raw).read_text(encoding="utf-8")
            except OSError as exc:
                raise SystemExit(f"cannot read batch input: {raw}") from exc

    source = source.lstrip("\ufeff")
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid batch JSON: {exc.msg}") from exc

    defaults: dict[str, dict[str, Any]] = {}
    if isinstance(parsed, list):
        actions = parsed
    elif isinstance(parsed, dict):
        unknown_keys = set(parsed) - {"defaults", "actions"}
        if unknown_keys:
            raise SystemExit(
                "batch object contains unsupported fields: "
                + ", ".join(sorted(unknown_keys))
            )
        actions = parsed.get("actions")
        raw_defaults = parsed.get("defaults", {})
        if not isinstance(actions, list):
            raise SystemExit("batch object actions must be a JSON array")
        if not isinstance(raw_defaults, dict):
            raise SystemExit("batch defaults must be an object keyed by tool name")
        for tool, arguments in raw_defaults.items():
            if not isinstance(tool, str) or not tool.strip():
                raise SystemExit("batch default tool names must be non-empty strings")
            if not isinstance(arguments, dict):
                raise SystemExit(
                    f"batch defaults for {tool!r} must be an arguments object"
                )
            defaults[tool.strip()] = dict(arguments)
    else:
        raise SystemExit("batch input must be a JSON array or an object with actions")

    if len(actions) > MAX_BATCH_ACTIONS:
        raise SystemExit(
            f"batch contains {len(actions)} actions; maximum is {MAX_BATCH_ACTIONS}"
        )
    if not all(isinstance(item, dict) for item in actions):
        raise SystemExit("every batch item must be a JSON object")

    normalized: list[dict[str, Any]] = []
    for item in actions:
        if item.get("type") == "wait":
            normalized.append(dict(item))
            continue

        tool = item.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            normalized.append(dict(item))
            continue
        tool = tool.strip()
        action_arguments = item.get("arguments", {})
        if not isinstance(action_arguments, dict):
            raise SystemExit("batch tool arguments must be an object")
        arguments = dict(defaults.get(tool, {}))
        arguments.update(action_arguments)
        normalized.append(
            {
                **item,
                "tool": tool,
                "arguments": arguments,
            }
        )
    return normalized


def _batch_payload_failed(payload: dict[str, Any]) -> bool:
    if payload.get("is_error"):
        return True
    structured = payload.get("structured_content")
    return isinstance(structured, dict) and structured.get("success") is False


def _validate_batch_action(item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    item_type = item.get("type")
    if item_type == "wait":
        milliseconds = item.get("milliseconds")
        if (
            isinstance(milliseconds, bool)
            or not isinstance(milliseconds, (int, float))
            or not math.isfinite(milliseconds)
            or milliseconds < 0
            or milliseconds > MAX_BATCH_WAIT_MS
        ):
            raise ValueError(
                "wait milliseconds must be between "
                f"0 and {MAX_BATCH_WAIT_MS}"
            )
        return "wait", "wait", {"milliseconds": milliseconds}

    tool = item.get("tool")
    arguments = item.get("arguments", {})
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("batch tool item requires a non-empty tool")
    if not isinstance(arguments, dict):
        raise ValueError("batch tool arguments must be an object")
    tool = tool.strip()
    return "tool", tool, {"arguments": arguments}


def _batch_dry_run(actions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    tool_counts: dict[str, int] = {}
    wait_count = 0

    for index, item in enumerate(actions):
        try:
            action_type, label, validated = _validate_batch_action(item)
        except ValueError as exc:
            return {
                "success": False,
                "dry_run": True,
                "error": str(exc),
                "failed_index": index,
                "total": len(actions),
                "validated": len(summaries),
                "actions": summaries,
            }

        if action_type == "wait":
            wait_count += 1
            summaries.append(
                {
                    "index": index,
                    "operation": label,
                    "type": "wait",
                    "milliseconds": validated["milliseconds"],
                }
            )
        else:
            tool_counts[label] = tool_counts.get(label, 0) + 1
            summaries.append(
                {
                    "index": index,
                    "operation": label,
                    "type": "tool",
                    "argument_keys": sorted(validated["arguments"].keys()),
                }
            )

    return {
        "success": True,
        "dry_run": True,
        "total": len(actions),
        "tool_calls": sum(tool_counts.values()),
        "waits": wait_count,
        "tools": dict(sorted(tool_counts.items())),
        "actions": summaries,
    }


def _release_batch_inputs(client: _DaemonHttpClient) -> bool:
    try:
        payload = client.request(
            "POST",
            "/call",
            payload={"name": "computer_release_all_inputs", "arguments": {}},
        )
    except RuntimeError:
        return False
    return not _batch_payload_failed(payload)


def _execute_batch(
    client: _DaemonHttpClient,
    actions: Sequence[dict[str, Any]],
    *,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    failed = 0
    released_inputs = False

    try:
        for index, item in enumerate(actions):
            item_started = time.monotonic()
            try:
                action_type, label, validated = _validate_batch_action(item)
                if action_type == "wait":
                    milliseconds = validated["milliseconds"]
                    time.sleep(float(milliseconds) / 1000.0)
                    payload: dict[str, Any] = {
                        "success": True,
                        "waited_ms": milliseconds,
                    }
                    item_failed = False
                else:
                    payload = client.request(
                        "POST",
                        "/call",
                        payload={"name": label, "arguments": validated["arguments"]},
                    )
                    item_failed = _batch_payload_failed(payload)
            except (RuntimeError, ValueError) as exc:
                label = str(item.get("tool") or item.get("type") or "invalid")
                payload = {"success": False, "error": str(exc)}
                item_failed = True

            elapsed_ms = round((time.monotonic() - item_started) * 1000, 2)
            results.append(
                {
                    "index": index,
                    "operation": label,
                    "success": not item_failed,
                    "duration_ms": elapsed_ms,
                    "result": payload,
                }
            )
            if item_failed:
                failed += 1
                released_inputs = _release_batch_inputs(client) or released_inputs
                if not continue_on_error:
                    break
    except KeyboardInterrupt:
        _release_batch_inputs(client)
        raise

    succeeded = len(results) - failed
    return {
        "success": failed == 0 and len(results) == len(actions),
        "completed": len(results) == len(actions),
        "total": len(actions),
        "processed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "released_inputs": released_inputs,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "results": results,
    }


def _is_lightweight_inprocess_call(tool_name: str, tool_args: dict[str, Any]) -> bool:
    if tool_name in LIGHTWEIGHT_INPROCESS_TOOLS:
        return True
    if tool_name in {"computer_recording", "computer_learn"}:
        return tool_args.get("action") == "status"
    return False


@contextmanager
def _mcp_import_mode(*, lightweight: bool):
    names = ("CUA_LIGHTWEIGHT_MCP_IMPORT", "CUA_SKIP_SOM_WARMUP")
    previous = {name: os.environ.get(name) for name in names}
    if lightweight:
        for name in names:
            os.environ[name] = "1"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _json_print(payload: Any, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{num_bytes} B"


def _check_dependency(import_name: str, distribution_name: str, purpose: str) -> DoctorCheck:
    spec = importlib.util.find_spec(import_name)
    if spec is None:
        return DoctorCheck(
            name=f"dependency:{distribution_name}",
            status="fail",
            message=f"{distribution_name} missing ({purpose})",
            details={"import": import_name},
        )
    try:
        version = importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        version = "installed"
    return DoctorCheck(
        name=f"dependency:{distribution_name}",
        status="ok",
        message=f"{distribution_name} {version}",
        details={"import": import_name, "purpose": purpose},
    )


def _check_state_dir() -> DoctorCheck:
    state_dir = _state_dir()
    probe_dir = state_dir if state_dir.exists() else state_dir.parent
    writable = probe_dir.exists() and os.access(probe_dir, os.W_OK)
    status = "ok" if writable else "fail"
    message = f"state dir {state_dir}" if writable else f"state dir is not writable: {state_dir}"
    return DoctorCheck(
        name="state_dir",
        status=status,
        message=message,
        details={"path": str(state_dir), "exists": state_dir.exists()},
    )


def _windows_desktop_name(handle: int) -> str | None:
    if sys.platform != "win32" or not handle:
        return None
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetUserObjectInformationW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    user32.GetUserObjectInformationW.restype = ctypes.c_bool

    UOI_NAME = 2
    needed = ctypes.c_ulong(0)
    user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
    if needed.value <= 0:
        return None
    char_count = max(1, needed.value // ctypes.sizeof(ctypes.c_wchar))
    buffer = ctypes.create_unicode_buffer(char_count)
    if not user32.GetUserObjectInformationW(
        handle,
        UOI_NAME,
        buffer,
        needed,
        ctypes.byref(needed),
    ):
        return None
    return buffer.value or None


def _check_interactive_desktop() -> DoctorCheck:
    if sys.platform != "win32":
        return DoctorCheck(
            name="desktop:interactive",
            status="fail",
            message="interactive desktop diagnostics require Windows",
            details={"platform": sys.platform},
        )
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    session_id = ctypes.c_ulong(0)
    session_known = bool(
        kernel32.ProcessIdToSessionId(
            os.getpid(),
            ctypes.byref(session_id),
        )
    )
    active_console = int(kernel32.WTSGetActiveConsoleSessionId())

    user32.GetThreadDesktop.argtypes = [ctypes.c_ulong]
    user32.GetThreadDesktop.restype = ctypes.c_void_p
    user32.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.CloseDesktop.restype = ctypes.c_bool

    current_desktop_handle = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    current_desktop = _windows_desktop_name(current_desktop_handle)

    DESKTOP_READOBJECTS = 0x0001
    input_desktop_handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    input_desktop = _windows_desktop_name(input_desktop_handle)
    if input_desktop_handle:
        user32.CloseDesktop(input_desktop_handle)

    sid = int(session_id.value) if session_known else None
    details = {
        "session_id": sid,
        "active_console_session_id": active_console
        if active_console != 0xFFFFFFFF
        else None,
        "current_desktop": current_desktop,
        "input_desktop": input_desktop,
    }
    if sid == 0:
        return DoctorCheck(
            name="desktop:interactive",
            status="fail",
            message="running in Windows Session 0; desktop automation requires an interactive user session",
            details=details,
        )
    if not session_known:
        return DoctorCheck(
            name="desktop:interactive",
            status="warn",
            message="could not determine Windows session id",
            details=details,
        )
    if not current_desktop or not input_desktop:
        return DoctorCheck(
            name="desktop:interactive",
            status="warn",
            message="could not inspect current or input desktop; screen may be locked or unavailable",
            details=details,
        )
    if current_desktop.lower() != input_desktop.lower():
        return DoctorCheck(
            name="desktop:interactive",
            status="warn",
            message=f"current desktop {current_desktop!r} differs from input desktop {input_desktop!r}",
            details=details,
        )
    if current_desktop.lower() != "default":
        return DoctorCheck(
            name="desktop:interactive",
            status="warn",
            message=f"running on non-default desktop {current_desktop!r}",
            details=details,
        )
    return DoctorCheck(
        name="desktop:interactive",
        status="ok",
        message=f"interactive session {sid} on desktop {current_desktop}",
        details=details,
    )


def _check_window_enumeration() -> DoctorCheck:
    if sys.platform != "win32":
        return DoctorCheck(
            name="desktop:windows",
            status="fail",
            message="window enumeration requires Windows",
            details={"platform": sys.platform},
        )
    try:
        import win32gui
    except Exception as exc:
        return DoctorCheck(
            name="desktop:windows",
            status="fail",
            message=f"win32gui import failed: {exc}",
        )

    total = 0
    visible = 0
    titled = 0
    samples: list[dict[str, Any]] = []

    def _enum(hwnd: int, _ctx: None) -> bool:
        nonlocal total, visible, titled
        total += 1
        try:
            is_visible = bool(win32gui.IsWindowVisible(hwnd))
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            return True
        if is_visible:
            visible += 1
        if title:
            titled += 1
        if is_visible and title and len(samples) < 8:
            samples.append({"hwnd": hwnd, "title": title[:120]})
        return True

    try:
        win32gui.EnumWindows(_enum, None)
        foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
        foreground_title = (
            win32gui.GetWindowText(foreground_hwnd) if foreground_hwnd else ""
        )
    except Exception as exc:
        return DoctorCheck(
            name="desktop:windows",
            status="fail",
            message=f"EnumWindows failed: {exc}",
            details={"total_windows": total, "visible_windows": visible},
        )

    details = {
        "total_windows": total,
        "visible_windows": visible,
        "titled_windows": titled,
        "foreground": {
            "hwnd": foreground_hwnd,
            "title": foreground_title[:120],
        },
        "samples": samples,
    }
    if visible == 0:
        return DoctorCheck(
            name="desktop:windows",
            status="warn",
            message="EnumWindows succeeded but no visible top-level windows were found",
            details=details,
        )
    return DoctorCheck(
        name="desktop:windows",
        status="ok",
        message=f"EnumWindows sees {visible} visible top-level windows",
        details=details,
    )


def _check_screenshot_capture() -> DoctorCheck:
    if sys.platform != "win32":
        return DoctorCheck(
            name="desktop:screenshot",
            status="fail",
            message="screenshot diagnostics require Windows",
            details={"platform": sys.platform},
        )
    started = time.perf_counter()
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(all_screens=True)
        width, height = image.size
    except Exception as exc:
        return DoctorCheck(
            name="desktop:screenshot",
            status="fail",
            message=f"full-screen capture failed: {exc}",
            details={"elapsed_ms": round((time.perf_counter() - started) * 1000, 1)},
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    details = {
        "width": width,
        "height": height,
        "mode": getattr(image, "mode", None),
        "elapsed_ms": elapsed_ms,
    }
    if width <= 1 or height <= 1:
        return DoctorCheck(
            name="desktop:screenshot",
            status="fail",
            message=f"full-screen capture returned invalid size {width}x{height}",
            details=details,
        )
    return DoctorCheck(
        name="desktop:screenshot",
        status="ok",
        message=f"full-screen capture works ({width}x{height}, {elapsed_ms} ms)",
        details=details,
    )


def _check_omniparser_weight() -> DoctorCheck:
    model_dir = OMNIPARSER_YOLO_DIR
    path = model_dir / "model.pt"
    manifest_path = model_dir / "model_manifest.json"
    if not path.exists():
        return DoctorCheck(
            name="model:omniparser_yolo",
            status="fail",
            message=f"OmniParser YOLO weight missing: {path}",
            details={"path": str(path)},
        )
    if not manifest_path.is_file():
        return DoctorCheck(
            name="model:omniparser_yolo",
            status="fail",
            message=f"OmniParser YOLO manifest missing: {manifest_path}",
            details={"path": str(manifest_path)},
        )
    size = path.stat().st_size
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = manifest["source"]
        expected_hash = manifest["sha256"]["model.pt"]
        if (
            not isinstance(source.get("repository"), str)
            or not isinstance(source.get("revision"), str)
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
        ):
            raise ValueError("required provenance fields are invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return DoctorCheck(
            name="model:omniparser_yolo",
            status="fail",
            message=f"OmniParser YOLO manifest is invalid: {exc}",
            details={"path": str(manifest_path)},
        )
    actual_hash = _sha256_file(path)
    if actual_hash.lower() != expected_hash.lower():
        return DoctorCheck(
            name="model:omniparser_yolo",
            status="fail",
            message="OmniParser YOLO weight checksum mismatch",
            details={
                "path": str(path),
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
            },
        )
    return DoctorCheck(
        name="model:omniparser_yolo",
        status="ok",
        message=f"OmniParser YOLO weight present ({_format_size(size)})",
        details={
            "path": str(path),
            "size_bytes": size,
            "repository": source["repository"],
            "revision": source["revision"],
            "verified_sha256": actual_hash,
        },
    )


def _check_florence2_bundle() -> DoctorCheck:
    model_dir = FLORENCE2_MODEL_DIR
    missing = [
        name for name in FLORENCE2_REQUIRED_FILES if not (model_dir / name).exists()
    ]
    if missing:
        return DoctorCheck(
            name="model:omniparser_florence2",
            status="fail",
            message=f"OmniParser Florence-2 bundle incomplete: missing {', '.join(missing)}",
            details={"path": str(model_dir), "missing": missing},
        )
    model_path = model_dir / "model.safetensors"
    model_size = model_path.stat().st_size
    if model_size < FLORENCE2_MIN_MODEL_BYTES:
        with model_path.open("rb") as handle:
            prefix = handle.read(128)
        is_lfs_pointer = prefix.startswith(
            b"version https://git-lfs.github.com/spec/v1"
        )
        reason = (
            "Git LFS pointer found; run `git lfs pull`"
            if is_lfs_pointer
            else f"model weight is unexpectedly small ({_format_size(model_size)})"
        )
        return DoctorCheck(
            name="model:omniparser_florence2",
            status="fail",
            message=f"OmniParser Florence-2 bundle incomplete: {reason}",
            details={
                "path": str(model_dir),
                "model_path": str(model_path),
                "size_bytes": model_size,
                "git_lfs_pointer": is_lfs_pointer,
            },
        )
    manifest_path = model_dir / "model_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        model_manifest = manifest["model"]
        expected_hash = manifest["sha256"]["model.safetensors"]
        if (
            not isinstance(model_manifest.get("repository"), str)
            or not isinstance(model_manifest.get("revision"), str)
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
        ):
            raise ValueError("required model provenance fields are invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return DoctorCheck(
            name="model:omniparser_florence2",
            status="fail",
            message=f"OmniParser Florence-2 manifest is invalid: {exc}",
            details={"path": str(manifest_path)},
        )
    total_size = sum(
        (model_dir / name).stat().st_size for name in FLORENCE2_REQUIRED_FILES
    )
    return DoctorCheck(
        name="model:omniparser_florence2",
        status="ok",
        message=f"OmniParser Florence-2 bundle present ({_format_size(total_size)})",
        details={
            "path": str(model_dir),
            "size_bytes": total_size,
            "repository": model_manifest["repository"],
            "revision": model_manifest["revision"],
            "expected_sha256": expected_hash,
        },
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_rapidocr_bundle() -> DoctorCheck:
    model_dir = RAPIDOCR_MODEL_DIR
    missing = [
        name for name in RAPIDOCR_REQUIRED_FILES if not (model_dir / name).is_file()
    ]
    if missing:
        return DoctorCheck(
            name="model:rapidocr_ppocrv4",
            status="fail",
            message=f"RapidOCR PP-OCRv4 bundle incomplete: missing {', '.join(missing)}",
            details={"path": str(model_dir), "missing": missing},
        )

    manifest_path = model_dir / "model_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_hashes = manifest["sha256"]
        source = manifest["source"]
        if (
            source.get("distribution") != "rapidocr"
            or not isinstance(source.get("version"), str)
            or not isinstance(expected_hashes, dict)
        ):
            raise ValueError("required provenance fields are invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return DoctorCheck(
            name="model:rapidocr_ppocrv4",
            status="fail",
            message=f"RapidOCR PP-OCRv4 manifest is invalid: {exc}",
            details={"path": str(manifest_path)},
        )

    verified: dict[str, str] = {}
    for name in RAPIDOCR_REQUIRED_FILES:
        if name == "model_manifest.json":
            continue
        path = model_dir / name
        expected = expected_hashes.get(name)
        if not isinstance(expected, str) or len(expected) != 64:
            return DoctorCheck(
                name="model:rapidocr_ppocrv4",
                status="fail",
                message=f"RapidOCR PP-OCRv4 manifest hash missing or invalid: {name}",
                details={"path": str(manifest_path), "file": name},
            )
        if path.suffix == ".onnx":
            with path.open("rb") as handle:
                prefix = handle.read(128)
            if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
                return DoctorCheck(
                    name="model:rapidocr_ppocrv4",
                    status="fail",
                    message=(
                        "RapidOCR PP-OCRv4 bundle incomplete: Git LFS pointer "
                        f"found for {name}; run `git lfs pull`"
                    ),
                    details={
                        "path": str(path),
                        "file": name,
                        "git_lfs_pointer": True,
                    },
                )
        actual = _sha256_file(path)
        if actual.lower() != expected.lower():
            return DoctorCheck(
                name="model:rapidocr_ppocrv4",
                status="fail",
                message=f"RapidOCR PP-OCRv4 checksum mismatch: {name}",
                details={
                    "path": str(path),
                    "file": name,
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                },
            )
        verified[name] = actual

    total_size = sum(
        (model_dir / name).stat().st_size
        for name in RAPIDOCR_REQUIRED_FILES
    )
    return DoctorCheck(
        name="model:rapidocr_ppocrv4",
        status="ok",
        message=f"RapidOCR PP-OCRv4 bundle present ({_format_size(total_size)})",
        details={
            "path": str(model_dir),
            "size_bytes": total_size,
            "source_version": source["version"],
            "verified_sha256": verified,
        },
    )


def _check_daemon(timeout: float) -> DoctorCheck:
    info = _load_daemon_info()
    if not info:
        return DoctorCheck(
            name="daemon",
            status="warn",
            message="daemon metadata not found; run `cua-win start` to start it",
        )
    status = _probe(info, timeout=timeout)
    if not status:
        return DoctorCheck(
            name="daemon",
            status="warn",
            message=f"daemon metadata exists but service is not reachable: {info.base_url}",
            details={
                "host": info.host,
                "port": info.port,
                "pid": info.pid,
                "metadata_file": str(info.metadata_file) if info.metadata_file else None,
            },
        )
    return DoctorCheck(
        name="daemon",
        status="ok",
        message=f"daemon running at {info.base_url}",
        details={
            "host": info.host,
            "port": info.port,
            "pid": info.pid,
            "tool_count": status.get("tool_count"),
        },
    )


def _doctor_summary(checks: Sequence[DoctorCheck]) -> dict[str, int]:
    return {
        "ok": sum(1 for check in checks if check.status == "ok"),
        "warn": sum(1 for check in checks if check.status == "warn"),
        "fail": sum(1 for check in checks if check.status == "fail"),
    }


def _collect_doctor_checks(
    *,
    timeout: float = 2.0,
    dependencies: bool = True,
    desktop: bool = True,
    daemon: bool = True,
) -> dict[str, Any]:
    checks: list[DoctorCheck] = []

    py_version = sys.version_info
    py_ok = (3, 12) <= (py_version.major, py_version.minor) < (3, 14)
    checks.append(
        DoctorCheck(
            name="python",
            status="ok" if py_ok else "fail",
            message=f"Python {platform.python_version()}",
            details={"executable": sys.executable, "supported": py_ok},
        )
    )

    checks.append(
        DoctorCheck(
            name="platform",
            status="ok" if sys.platform == "win32" else "fail",
            message=f"platform {sys.platform}",
            details={"system": platform.platform()},
        )
    )

    checks.append(_check_state_dir())
    checks.append(_check_omniparser_weight())
    checks.append(_check_florence2_bundle())
    checks.append(_check_rapidocr_bundle())

    if desktop:
        checks.extend(
            (
                _check_interactive_desktop(),
                _check_window_enumeration(),
                _check_screenshot_capture(),
            )
        )
    if dependencies:
        checks.extend(
            _check_dependency(import_name, distribution_name, purpose)
            for import_name, distribution_name, purpose in DEPENDENCY_CHECKS
        )
    if daemon:
        checks.append(_check_daemon(timeout=timeout))

    summary = _doctor_summary(checks)
    return {
        "ok": summary["fail"] == 0,
        "summary": summary,
        "checks": [check.to_json() for check in checks],
    }


def _format_doctor(payload: dict[str, Any]) -> None:
    for check in payload["checks"]:
        label = check["status"].upper()
        print(f"[{label:4s}] {check['name']}: {check['message']}")
    summary = payload["summary"]
    print(
        f"\nSummary: {summary['ok']} ok, {summary['warn']} warn, {summary['fail']} fail"
    )


def _format_tools(payload: dict[str, Any], json_mode: bool) -> None:
    tools = payload.get("tools", [])
    if json_mode:
        _json_print(payload)
        return
    for tool in tools:
        name = tool.get("name", "")
        description = str(tool.get("description") or "").splitlines()[0][:90]
        print(f"{name:45s} {description}")
    print(f"\n{payload.get('count', len(tools))} tools total")


def _format_call(payload: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        _json_print(payload)
        return
    structured = payload.get("structured_content")
    if structured is not None:
        _json_print(structured)
        return
    _json_print(payload)


async def _inprocess_list_tools() -> dict[str, Any]:
    with _mcp_import_mode(lightweight=True):
        from .mcp_server import create_mcp_server

        mcp = create_mcp_server()
        tools = [_tool_to_json(tool) for tool in await mcp.list_tools()]
    return {"tools": tools, "count": len(tools), "mode": "in-process"}


async def _inprocess_describe(tool_name: str) -> dict[str, Any]:
    payload = await _inprocess_list_tools()
    for tool in payload["tools"]:
        if tool.get("name") == tool_name:
            return {"tool": tool, "mode": "in-process"}
    raise SystemExit(f"unknown tool: {tool_name}")


async def _inprocess_call(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
    lightweight = _is_lightweight_inprocess_call(tool_name, tool_args)
    with _mcp_import_mode(lightweight=lightweight):
        from .mcp_server import create_mcp_server

        mcp = create_mcp_server()
        result = await mcp.call_tool(tool_name, tool_args)
    payload = _call_result_to_json(result)
    payload.update({"tool": tool_name, "mode": "in-process"})
    return payload


def _rewrite_implicit_call(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    if args and not args[0].startswith("-") and args[0] not in COMMANDS:
        return ["call", *args]
    return args


async def _serve_async(
    host: str,
    port: int,
    token: str | None,
    metadata_file: Path | None,
    quiet: bool,
    log_level: str,
    ssl_keyfile: str | None,
    ssl_certfile: str | None,
    i_know_this_exposes_my_computer: bool,
    enable_cli_rpc: bool,
    target_width: int | None,
    target_height: int | None,
    detect_resolution: bool,
) -> None:
    if sys.platform != "win32":
        raise SystemExit("cua-pywin only supports Windows")
    if host not in LOCAL_HOSTS and not i_know_this_exposes_my_computer:
        raise SystemExit(
            "Refusing to bind desktop-control HTTP server to a non-localhost host "
            "without --i-know-this-exposes-my-computer"
        )
    if port < 0 or port > 65535:
        raise SystemExit("port must be in range 0..65535")
    if port == 0:
        port = _find_free_port(DEFAULT_HOST)
    if (target_width is None) != (target_height is None):
        raise SystemExit("--width and --height must be provided together")
    if target_width is not None and target_width <= 0:
        raise SystemExit("--width must be positive")
    if target_height is not None and target_height <= 0:
        raise SystemExit("--height must be positive")

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    if host not in LOCAL_HOSTS:
        logging.getLogger(__name__).warning(
            "Binding desktop-control HTTP server to %s exposes it to the network.",
            host,
        )

    metadata_file = metadata_file or _metadata_path()
    if enable_cli_rpc:
        token = token or secrets.token_urlsafe(32)
        os.environ[DAEMON_TOKEN_ENV] = token
    else:
        token = ""
        os.environ.pop(DAEMON_TOKEN_ENV, None)

    ssl_args: dict[str, str] = {}
    if ssl_keyfile and ssl_certfile:
        ssl_args = {"ssl_keyfile": ssl_keyfile, "ssl_certfile": ssl_certfile}
    elif ssl_keyfile or ssl_certfile:
        logging.getLogger(__name__).warning(
            "Both --ssl-keyfile and --ssl-certfile are required for HTTPS; running HTTP."
        )

    if target_width or target_height or detect_resolution:
        from .mcp_server import _configure_scaling

        _configure_scaling(
            target_width=target_width,
            target_height=target_height,
            detect_resolution=detect_resolution,
        )

    import uvicorn
    from .main import app

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning" if quiet else log_level,
        access_log=not quiet,
        **ssl_args,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server

    info = None
    if enable_cli_rpc:
        info = DaemonInfo(
            host=host,
            port=port,
            token=token,
            pid=os.getpid(),
            metadata_file=metadata_file,
        )
        _write_json(metadata_file, _metadata_payload(info))
    if not quiet:
        protocol = "https" if ssl_args else "http"
        print(f"{CLI_NAME} serving {protocol}://{host}:{port}", flush=True)

    try:
        await server.serve()
    finally:
        if enable_cli_rpc:
            data = _read_json(metadata_file)
            current = None
            if data:
                try:
                    current = DaemonInfo(
                        host=str(data["host"]),
                        port=int(data["port"]),
                        token=str(data["token"]),
                        pid=int(data["pid"]) if data.get("pid") is not None else None,
                        metadata_file=metadata_file,
                    )
                except (KeyError, TypeError, ValueError):
                    current = None
            if (
                current
                and current.pid == os.getpid()
                and current.port == port
                and current.token == token
            ):
                _remove_metadata(metadata_file)


def _cmd_serve(args: argparse.Namespace) -> int:
    metadata_file = Path(args.metadata_file) if args.metadata_file else None
    asyncio.run(
        _serve_async(
            host=args.host,
            port=args.port,
            token=args.token,
            metadata_file=metadata_file,
            quiet=args.quiet,
            log_level=args.log_level,
            ssl_keyfile=args.ssl_keyfile,
            ssl_certfile=args.ssl_certfile,
            i_know_this_exposes_my_computer=args.i_know_this_exposes_my_computer,
            enable_cli_rpc=not args.no_cli_rpc,
            target_width=args.width,
            target_height=args.height,
            detect_resolution=args.detect_resolution,
        )
    )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    info = _ensure_daemon(timeout=args.timeout)
    payload = _probe(info) or {"status": "unknown"}
    payload.update({"host": info.host, "port": info.port, "pid": info.pid})
    if args.json:
        _json_print(payload)
    else:
        print(f"running: {info.base_url} pid={info.pid} tools={payload.get('tool_count', '?')}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    info = _load_daemon_info()
    if not info:
        print("stopped")
        return 0
    status = _probe(info)
    if not status:
        _remove_metadata(info.metadata_file)
        print("stopped")
        return 0
    status.update({"host": info.host, "port": info.port, "pid": info.pid})
    if args.json:
        _json_print(status)
    else:
        print(f"running: {info.base_url} pid={info.pid} tools={status.get('tool_count', '?')}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    info = _load_daemon_info()
    if not info or not _probe(info):
        if info:
            _remove_metadata(info.metadata_file)
        print("stopped")
        return 0
    _http_json(info, "POST", "/shutdown", payload={}, timeout=5)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not _probe(info, timeout=0.5):
            _remove_metadata(info.metadata_file)
            print("stopped")
            return 0
        time.sleep(0.2)
    raise SystemExit("daemon did not stop before timeout")


def _cmd_doctor(args: argparse.Namespace) -> int:
    payload = _collect_doctor_checks(
        timeout=args.timeout,
        dependencies=not args.no_dependencies,
        desktop=not args.no_desktop,
        daemon=not args.no_daemon,
    )
    if args.json:
        _json_print(payload)
    else:
        _format_doctor(payload)
    return 0 if payload["ok"] else 1


def _cmd_list_tools(args: argparse.Namespace) -> int:
    if args.no_daemon:
        payload = asyncio.run(_inprocess_list_tools())
    else:
        info = _ensure_daemon(timeout=args.start_timeout)
        payload = _http_json(info, "GET", "/tools", timeout=args.timeout)
    _format_tools(payload, json_mode=args.json)
    return 0


def _cmd_describe(args: argparse.Namespace) -> int:
    if args.no_daemon:
        payload = asyncio.run(_inprocess_describe(args.tool))
    else:
        info = _ensure_daemon(timeout=args.start_timeout)
        name = urllib.parse.quote(args.tool, safe="")
        payload = _http_json(info, "GET", f"/tools/{name}", timeout=args.timeout)
    _json_print(payload)
    return 0


def _cmd_call(args: argparse.Namespace) -> int:
    tool_args = _parse_tool_arguments(args.arguments)
    if args.no_daemon:
        payload = asyncio.run(_inprocess_call(args.tool, tool_args))
    else:
        info = _ensure_daemon(timeout=args.start_timeout)
        payload = _http_json(
            info,
            "POST",
            "/call",
            payload={"name": args.tool, "arguments": tool_args},
            timeout=args.timeout,
        )
    _format_call(payload, json_mode=args.json)
    return 1 if payload.get("is_error") else 0


def _cmd_batch(args: argparse.Namespace) -> int:
    actions = _parse_batch_actions(args.input)
    if args.dry_run:
        payload = _batch_dry_run(actions)
        _json_print(payload)
        return 0 if payload["success"] else 1

    info = _ensure_daemon(timeout=args.start_timeout)
    client = _DaemonHttpClient(info, timeout=args.timeout, batch_mode=True)
    try:
        payload = _execute_batch(
            client,
            actions,
            continue_on_error=args.continue_on_error,
        )
    except KeyboardInterrupt:
        print("batch interrupted", file=sys.stderr)
        return 130
    finally:
        client.close()
    _json_print(payload)
    return 0 if payload["success"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description="CLI for the cua-pywin HTTP server and MCP tool calls.",
        formatter_class=_HelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser(
        "serve",
        help="run the HTTP server in the foreground",
        formatter_class=_HelpFormatter,
    )
    serve.add_argument("--width", type=int, help="Target screenshot width for coordinate scaling")
    serve.add_argument("--height", type=int, help="Target screenshot height for coordinate scaling")
    serve.add_argument(
        "--detect-resolution",
        action="store_true",
        help="Auto-detect and log the actual screen resolution at startup",
    )
    serve.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Host interface to bind.",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="TCP port to bind.",
    )
    serve.add_argument(
        "--i-know-this-exposes-my-computer",
        action="store_true",
        help="Required when binding to a non-localhost host.",
    )
    serve.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="HTTP server log level.",
    )
    serve.add_argument("--ssl-keyfile", help="Path to SSL private key file")
    serve.add_argument("--ssl-certfile", help="Path to SSL certificate file")
    serve.add_argument(
        "--no-cli-rpc",
        action="store_true",
        help="Disable cua-win JSON RPC routes and metadata for this foreground server.",
    )
    serve.add_argument("--token", help=argparse.SUPPRESS)
    serve.add_argument("--metadata-file", help=argparse.SUPPRESS)
    serve.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress normal startup output.",
    )
    serve.set_defaults(func=_cmd_serve)

    start = subparsers.add_parser(
        "start",
        help="start the background daemon",
        formatter_class=_HelpFormatter,
    )
    start.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT,
        help="Seconds to wait for the daemon to become ready.",
    )
    start.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    start.set_defaults(func=_cmd_start)

    status = subparsers.add_parser(
        "status",
        help="show daemon status",
        formatter_class=_HelpFormatter,
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    status.set_defaults(func=_cmd_status)

    stop = subparsers.add_parser(
        "stop",
        help="stop the background daemon",
        formatter_class=_HelpFormatter,
    )
    stop.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the daemon to exit.",
    )
    stop.set_defaults(func=_cmd_stop)

    doctor = subparsers.add_parser(
        "doctor",
        help="diagnose local cua-win runtime health",
        formatter_class=_HelpFormatter,
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    doctor.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Seconds allowed for the daemon health probe.",
    )
    doctor.add_argument(
        "--no-dependencies",
        action="store_true",
        help="Skip dependency import-spec checks.",
    )
    doctor.add_argument(
        "--no-desktop",
        action="store_true",
        help="Skip interactive desktop, window enumeration, and screenshot probes.",
    )
    doctor.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip probing the existing background daemon.",
    )
    doctor.set_defaults(func=_cmd_doctor)

    list_tools = subparsers.add_parser(
        "list-tools",
        help="list MCP tools",
        formatter_class=_HelpFormatter,
    )
    list_tools.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    list_tools.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds allowed for the HTTP request.",
    )
    list_tools.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT,
        help="Seconds to wait when auto-starting the daemon.",
    )
    list_tools.add_argument(
        "--no-daemon",
        action="store_true",
        help="Run once in-process instead of reusing the daemon.",
    )
    list_tools.set_defaults(func=_cmd_list_tools)

    describe = subparsers.add_parser(
        "describe",
        help="show one tool schema",
        formatter_class=_HelpFormatter,
    )
    describe.add_argument("tool", help="MCP tool name to describe.")
    describe.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds allowed for the HTTP request.",
    )
    describe.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT,
        help="Seconds to wait when auto-starting the daemon.",
    )
    describe.add_argument(
        "--no-daemon",
        action="store_true",
        help="Run once in-process instead of reusing the daemon.",
    )
    describe.set_defaults(func=_cmd_describe)

    call = subparsers.add_parser(
        "call",
        help="call one MCP tool",
        formatter_class=_HelpFormatter,
    )
    call.add_argument("tool", help="MCP tool name to call.")
    call.add_argument(
        "arguments",
        nargs="?",
        default="{}",
        help=(
            "Tool arguments as inline JSON, '-' for UTF-8 stdin, or "
            "'@path' for a UTF-8 JSON file."
        ),
    )
    call.add_argument(
        "--json",
        action="store_true",
        help="Print the full machine-readable call envelope.",
    )
    call.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds allowed for the tool HTTP request.",
    )
    call.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT,
        help="Seconds to wait when auto-starting the daemon.",
    )
    call.add_argument(
        "--no-daemon",
        action="store_true",
        help=(
            "Run once in-process instead of reusing the daemon; heavy tools may "
            "pay model import and warm-up costs."
        ),
    )
    call.set_defaults(func=_cmd_call)

    batch = subparsers.add_parser(
        "batch",
        help="execute a JSON action array or {defaults, actions} object",
        description="""Execute mixed MCP tool calls and waits sequentially.

Input may be a simple action array or an object containing per-tool defaults
and an actions array. Defaults are shallow-merged by tool name; each action's
arguments override its defaults. Wait entries never use defaults.

Execution stops on the first failure unless --continue-on-error is set.
Use --dry-run to validate and summarize the expanded actions without starting
the daemon or sending input.
Batch requests suppress the agent cursor trajectory overlay, while real input
timing such as computer_drag duration is preserved.""",
        epilog="""Examples:
  cua-win batch '[{"tool":"computer_get_screen_size","arguments":{}},{"type":"wait","milliseconds":100}]'

  '{"defaults":{"computer_drag":{"duration":0.02,"coordinate_space":"window_resized"}},"actions":[{"tool":"computer_drag","arguments":{"start_x":10,"start_y":20,"end_x":100,"end_y":20}},{"tool":"computer_drag","arguments":{"start_x":10,"start_y":22,"end_x":100,"end_y":22}}]}' | cua-win batch""",
        formatter_class=_HelpFormatter,
    )
    batch.add_argument(
        "input",
        nargs="?",
        default="-",
        help=(
            "Inline JSON array/object, UTF-8 JSON file path, or '-' to read "
            "stdin."
        ),
    )
    batch.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue after a failed batch item instead of stopping.",
    )
    batch.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and summarize the expanded batch without starting the daemon.",
    )
    batch.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds allowed for each tool HTTP request.",
    )
    batch.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT,
        help="Seconds to wait when auto-starting the daemon.",
    )
    batch.set_defaults(func=_cmd_batch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    rewritten = _rewrite_implicit_call(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(rewritten)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
