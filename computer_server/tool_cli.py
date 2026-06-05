"""Daemon-first command line client for local MCP tool calls."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import logging
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
    DAEMON_TOKEN_ENV,
    call_result_to_json as _call_result_to_json,
    tool_to_json as _tool_to_json,
)


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
    ("transformers", "transformers", "TinyCLIP"),
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

TINYCLIP_REQUIRED_FILES = (
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer.json",
)


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
    args = [
        _daemon_python_executable(),
        "-m",
        "computer_server.tool_cli",
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


def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON arguments: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("tool arguments must be a JSON object")
    return parsed


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


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def _check_omniparser_weight() -> DoctorCheck:
    path = _project_root() / "som" / "weights" / "icon_detect" / "model.pt"
    if not path.exists():
        return DoctorCheck(
            name="model:omniparser_yolo",
            status="fail",
            message=f"OmniParser YOLO weight missing: {path}",
            details={"path": str(path)},
        )
    size = path.stat().st_size
    if size <= 0:
        return DoctorCheck(
            name="model:omniparser_yolo",
            status="fail",
            message=f"OmniParser YOLO weight is empty: {path}",
            details={"path": str(path), "size_bytes": size},
        )
    return DoctorCheck(
        name="model:omniparser_yolo",
        status="ok",
        message=f"OmniParser YOLO weight present ({_format_size(size)})",
        details={"path": str(path), "size_bytes": size},
    )


def _check_tinyclip_bundle() -> DoctorCheck:
    model_dir = (
        Path(__file__).resolve().parent
        / "models"
        / "tinyclip"
        / "wkcn-TinyCLIP-ViT-8M-16-Text-3M-YFCC15M"
    )
    missing = [name for name in TINYCLIP_REQUIRED_FILES if not (model_dir / name).exists()]
    if missing:
        return DoctorCheck(
            name="model:tinyclip",
            status="fail",
            message=f"TinyCLIP bundle incomplete: missing {', '.join(missing)}",
            details={"path": str(model_dir), "missing": missing},
        )
    total_size = sum((model_dir / name).stat().st_size for name in TINYCLIP_REQUIRED_FILES)
    return DoctorCheck(
        name="model:tinyclip",
        status="ok",
        message=f"TinyCLIP bundle present ({_format_size(total_size)})",
        details={"path": str(model_dir), "size_bytes": total_size},
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
    checks.append(_check_tinyclip_bundle())

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description="CLI for the cua-pywin HTTP server and MCP tool calls.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the HTTP server in the foreground")
    serve.add_argument("--width", type=int, help="Target screenshot width for coordinate scaling")
    serve.add_argument("--height", type=int, help="Target screenshot height for coordinate scaling")
    serve.add_argument(
        "--detect-resolution",
        action="store_true",
        help="Auto-detect and log the actual screen resolution at startup",
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--i-know-this-exposes-my-computer",
        action="store_true",
        help="Required when binding to a non-localhost host.",
    )
    serve.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
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
    serve.add_argument("--quiet", action="store_true")
    serve.set_defaults(func=_cmd_serve)

    start = subparsers.add_parser("start", help="start the background daemon")
    start.add_argument("--timeout", type=float, default=DEFAULT_START_TIMEOUT)
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=_cmd_start)

    status = subparsers.add_parser("status", help="show daemon status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    stop = subparsers.add_parser("stop", help="stop the background daemon")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.set_defaults(func=_cmd_stop)

    doctor = subparsers.add_parser("doctor", help="diagnose local cua-win runtime health")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--timeout", type=float, default=2.0)
    doctor.add_argument(
        "--no-dependencies",
        action="store_true",
        help="Skip dependency import-spec checks.",
    )
    doctor.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip probing the existing background daemon.",
    )
    doctor.set_defaults(func=_cmd_doctor)

    list_tools = subparsers.add_parser("list-tools", help="list MCP tools")
    list_tools.add_argument("--json", action="store_true")
    list_tools.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    list_tools.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT)
    list_tools.add_argument("--no-daemon", action="store_true")
    list_tools.set_defaults(func=_cmd_list_tools)

    describe = subparsers.add_parser("describe", help="show one tool schema")
    describe.add_argument("tool")
    describe.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    describe.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT)
    describe.add_argument("--no-daemon", action="store_true")
    describe.set_defaults(func=_cmd_describe)

    call = subparsers.add_parser("call", help="call one MCP tool")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}")
    call.add_argument("--json", action="store_true")
    call.add_argument("--timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    call.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT)
    call.add_argument("--no-daemon", action="store_true")
    call.set_defaults(func=_cmd_call)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    rewritten = _rewrite_implicit_call(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(rewritten)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
