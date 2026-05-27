"""Runtime capability and path permission checks for MCP tools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


class PermissionDenied(PermissionError):
    """Raised when a runtime capability is disabled by policy."""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def resolve_path(path: str) -> Path:
    """Resolve a path to an absolute filesystem path."""
    return Path(path).expanduser().resolve()


def configured_file_roots() -> list[Path]:
    roots = os.environ.get("CUA_FILE_ROOTS", "")
    result: list[Path] = []
    for raw in roots.split(os.pathsep):
        raw = raw.strip()
        if raw:
            result.append(resolve_path(raw))
    return result


def dangerous_file_tools_disabled() -> bool:
    return _env_flag("CUA_DISABLE_DANGEROUS_FILE_TOOLS")


def path_in_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_allowed_path(path: str, operation: str, dangerous: bool = False) -> Path:
    """Resolve and validate file-tool paths against optional allowlist roots."""
    if dangerous and dangerous_file_tools_disabled():
        raise PermissionDenied(
            f"{operation} is disabled by CUA_DISABLE_DANGEROUS_FILE_TOOLS"
        )

    resolved = resolve_path(path)
    roots = configured_file_roots()
    if roots and not any(path_in_root(resolved, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise PermissionDenied(
            f"Path is outside configured CUA_FILE_ROOTS for {operation}: "
            f"{resolved}. Allowed roots: {allowed}"
        )
    return resolved


def audit_path(path: Path, operation: str) -> Dict[str, Any]:
    roots = configured_file_roots()
    return {
        "path": str(path),
        "operation": operation,
        "root_restricted": bool(roots),
    }


@dataclass(frozen=True)
class RuntimePermissionPolicy:
    """Runtime capability policy derived from environment variables."""

    run_command_enabled: bool = True
    local_open_enabled: bool = True
    cdp_javascript_enabled: bool = True

    @classmethod
    def from_env(cls) -> "RuntimePermissionPolicy":
        return cls(
            run_command_enabled=not _env_flag("CUA_DISABLE_RUN_COMMAND"),
            local_open_enabled=not _env_flag("CUA_DISABLE_LOCAL_OPEN"),
            cdp_javascript_enabled=not _env_flag("CUA_DISABLE_CDP_JAVASCRIPT"),
        )

    def require_run_command(self) -> None:
        if not self.run_command_enabled:
            raise PermissionDenied("computer_run_command is disabled by CUA_DISABLE_RUN_COMMAND")

    def require_local_open(self, path: str) -> Path:
        if not self.local_open_enabled:
            raise PermissionDenied("local file open is disabled by CUA_DISABLE_LOCAL_OPEN")
        return resolve_allowed_path(path, "open")

    def require_cdp_javascript(self) -> None:
        if not self.cdp_javascript_enabled:
            raise PermissionDenied(
                "computer_page execute_javascript is disabled by CUA_DISABLE_CDP_JAVASCRIPT"
            )
