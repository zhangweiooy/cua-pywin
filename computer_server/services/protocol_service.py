"""Public cua-win 0.2 protocol boundary.

Internal services intentionally keep their existing dictionaries. This module
owns the one-way projection into the reset public MCP/HTTP/CLI contract.
"""

from __future__ import annotations

import base64
import copy
import logging
import mimetypes
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping, Sequence

from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from pydantic import ValidationError as PydanticValidationError

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "0.2"

ACTION_OUTCOMES = frozenset({"completed", "rejected", "unknown"})
RETRY_SAFETY_VALUES = frozenset(
    {"not_needed", "safe", "reobserve", "do_not_retry"}
)

STABLE_ERROR_CODES = frozenset(
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

_READ_ONLY_TOOLS = frozenset(
    {
        "computer_screenshot",
        "computer_get_screen_size",
        "computer_get_cursor_position",
        "computer_get_window",
        "computer_list_apps",
        "computer_list_windows",
        "computer_get_desktop_environment",
        "computer_get_window_state",
        "computer_zoom",
    }
)

_ERROR_CODE_ALIASES = {
    "invalid_argument": "INVALID_ARGUMENT",
    "validation_error": "INVALID_ARGUMENT",
    "target_required": "TARGET_REQUIRED",
    "target_not_found": "TARGET_NOT_FOUND",
    "target_mismatch": "TARGET_MISMATCH",
    "target_activation_failed": "ACTION_REJECTED",
    "snapshot_not_found": "OBSERVATION_NOT_FOUND",
    "observation_not_found": "OBSERVATION_NOT_FOUND",
    "screenshot_not_found": "OBSERVATION_NOT_FOUND",
    "snapshot_stale": "STALE_OBSERVATION",
    "observation_stale": "STALE_OBSERVATION",
    "screenshot_stale": "STALE_OBSERVATION",
    "observation_target_mismatch": "OBSERVATION_TARGET_MISMATCH",
    "permission_denied": "PERMISSION_DENIED",
    "unsupported_operation": "UNSUPPORTED_OPERATION",
    "provider_unavailable": "PROVIDER_UNAVAILABLE",
    "uia_unavailable": "PROVIDER_UNAVAILABLE",
    "capture_failed": "CAPTURE_FAILED",
    "screenshot_failed": "CAPTURE_FAILED",
    "action_rejected": "ACTION_REJECTED",
    "action_outcome_unknown": "ACTION_OUTCOME_UNKNOWN",
    "post_action_verification_failed": "POSTCONDITION_FAILED",
    "postcondition_failed": "POSTCONDITION_FAILED",
}

_PUBLIC_REMOVED_KEYS = frozenset(
    {
        "window_id",
        "windowId",
        "parent_window_id",
        "hwnd",
        "window_ref",
        "screenshot_out_file",
        "debug_image_out",
        "base64_image",
        "image_data",
    }
)


@dataclass
class ActionExecutionContext:
    """Ephemeral action state for one tool invocation."""

    tool_name: str
    action_id: str
    dispatch_started: bool = False
    completed: bool = False

    def mark_dispatch_started(self) -> None:
        self.dispatch_started = True

    def mark_completed(self) -> None:
        self.completed = True


_ACTION_CONTEXT: ContextVar[ActionExecutionContext | None] = ContextVar(
    "cua_action_context",
    default=None,
)


class ProtocolValidationMiddleware(Middleware):
    """Convert expected tool argument failures into normal 0.2 results."""

    async def on_call_tool(
        self,
        context: Any,
        call_next: Any,
    ) -> ToolResult:
        try:
            return await call_next(context)
        except PydanticValidationError as exc:
            message = context.message
            tool_name = str(message.name)
            arguments = dict(message.arguments or {})
            return validation_failure_result(
                _validation_error_message(exc),
                action_id=begin_action(tool_name, arguments),
            )


def is_side_effect_call(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    """Return whether this invocation can produce an external side effect."""
    if tool_name in _READ_ONLY_TOOLS:
        return False
    if tool_name == "computer_page":
        return str(arguments.get("action") or "").lower() == "execute_javascript"
    if tool_name in {"computer_recording", "computer_learn"}:
        return str(arguments.get("action") or "status").lower() != "status"
    return True


def begin_action(tool_name: str, arguments: Mapping[str, Any]) -> str | None:
    if not is_side_effect_call(tool_name, arguments):
        return None
    action_id = f"act-{uuid.uuid4().hex}"
    logger.info("action accepted tool=%s action_id=%s", tool_name, action_id)
    return action_id


def begin_action_context(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> ActionExecutionContext | None:
    action_id = begin_action(tool_name, arguments)
    if action_id is None:
        return None
    return ActionExecutionContext(tool_name=tool_name, action_id=action_id)


@contextmanager
def action_execution_scope(
    action_context: ActionExecutionContext | None,
) -> Iterator[None]:
    token: Token[ActionExecutionContext | None] = _ACTION_CONTEXT.set(
        action_context
    )
    try:
        yield
    finally:
        _ACTION_CONTEXT.reset(token)


def current_action_context() -> ActionExecutionContext | None:
    return _ACTION_CONTEXT.get()


def mark_action_dispatch_started() -> None:
    action_context = current_action_context()
    if action_context is not None:
        action_context.mark_dispatch_started()


def log_unknown_action(tool_name: str, action_id: str | None) -> None:
    if action_id:
        logger.exception(
            "action transport failure tool=%s action_id=%s outcome=unknown",
            tool_name,
            action_id,
        )


def protocol_error(
    code: str,
    message: str,
    *,
    suggestion: str | None = None,
    retry_safety: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_error_code(code, message)
    retry = retry_safety or _retry_safety_for_error(normalized)
    if retry not in RETRY_SAFETY_VALUES:
        raise ValueError(f"invalid retry_safety: {retry}")
    return {
        "code": normalized,
        "message": str(message),
        "suggestion": suggestion,
        "retry_safety": retry,
        "details": _scrub_public(dict(details or {})),
    }


def failure_envelope(
    code: str,
    message: str,
    *,
    suggestion: str | None = None,
    retry_safety: str | None = None,
    details: Mapping[str, Any] | None = None,
    action: Mapping[str, Any] | None = None,
    warnings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "success": False,
        "data": {},
        "artifacts": [],
        "warnings": list(warnings or []),
        "error": protocol_error(
            code,
            message,
            suggestion=suggestion,
            retry_safety=retry_safety,
            details=details,
        ),
        "action": dict(action) if action is not None else None,
    }


def validation_failure_result(
    message: str,
    *,
    action_id: str | None = None,
) -> ToolResult:
    """Build a normal MCP result for validation rejected before a tool call."""
    return ToolResult(
        structured_content=failure_envelope(
            "INVALID_ARGUMENT",
            message,
            retry_safety="safe",
            action=(
                {
                    "action_id": action_id,
                    "outcome": "rejected",
                    "retry_safety": "safe",
                }
                if action_id
                else None
            ),
        )
    )


def protocolize_tool_result(
    raw_result: Any,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    action_id: str | None = None,
    action_context: ActionExecutionContext | None = None,
) -> ToolResult:
    """Project one internal result into the fixed 0.2 ToolResult contract."""
    side_effect = is_side_effect_call(tool_name, arguments)
    if action_context is not None:
        action_id = action_context.action_id
    if side_effect and action_id is None:
        action_id = begin_action(tool_name, arguments)

    content: list[Any] = []
    raw_data: Any = raw_result
    if isinstance(raw_result, ToolResult):
        raw_data = raw_result.structured_content or {}
        for item in raw_result.content or []:
            if getattr(item, "type", None) == "image":
                content.append(item)

    if isinstance(raw_data, Mapping):
        legacy = copy.deepcopy(dict(raw_data))
    elif raw_data is None:
        legacy = {}
    elif isinstance(raw_data, list):
        legacy = {"items": copy.deepcopy(raw_data)}
    else:
        legacy = {"value": copy.deepcopy(raw_data)}

    success = legacy.get("success") is not False
    warnings = _collect_warnings(legacy)
    artifacts: list[dict[str, Any]] = []

    existing_image_meta = dict(legacy)
    for item in content:
        _append_existing_image_artifact(
            item,
            artifacts,
            existing_image_meta,
            tool_name=tool_name,
            content_index=len(artifacts),
        )

    _extract_legacy_images(
        legacy,
        content,
        artifacts,
        tool_name=tool_name,
    )

    error: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    if not success:
        message, suggestion, source_code, details = _legacy_error_parts(legacy)
        code = _normalize_error_code(source_code, message)
        if (
            side_effect
            and action_id
            and code == "ACTION_REJECTED"
            and not source_code.strip()
            and (
                action_context is None
                or action_context.dispatch_started
            )
        ):
            code = "ACTION_OUTCOME_UNKNOWN"
            suggestion = suggestion or (
                "Reobserve the target before deciding whether to retry."
            )
        retry_safety = _retry_safety_for_error(code)
        error = protocol_error(
            code,
            message,
            suggestion=suggestion,
            retry_safety=retry_safety,
            details=details,
        )
        if side_effect and action_id:
            if code == "POSTCONDITION_FAILED":
                outcome = "completed"
            elif code == "ACTION_OUTCOME_UNKNOWN":
                outcome = "unknown"
            elif code == "ACTION_REJECTED":
                outcome = "rejected"
            elif (
                action_context is not None
                and action_context.dispatch_started
            ):
                outcome = "unknown"
                code = "ACTION_OUTCOME_UNKNOWN"
                retry_safety = "reobserve"
                error = protocol_error(
                    code,
                    message,
                    suggestion=(
                        suggestion
                        or "Reobserve the target before deciding whether to retry."
                    ),
                    retry_safety=retry_safety,
                    details=details,
                )
            else:
                outcome = "rejected"
            action = {
                "action_id": action_id,
                "outcome": outcome,
                "retry_safety": retry_safety,
            }
    elif side_effect and action_id:
        if action_context is not None:
            action_context.mark_completed()
        action = {
            "action_id": action_id,
            "outcome": "completed",
            "retry_safety": "not_needed",
        }

    if success:
        _append_file_artifacts(tool_name, legacy, artifacts)
    data = _project_data(
        legacy,
        success=success,
        tool_name=tool_name,
    )
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "success": success,
        "data": data,
        "artifacts": artifacts,
        "warnings": warnings,
        "error": error,
        "action": action,
    }
    if action_id:
        logger.info(
            "action finished tool=%s action_id=%s outcome=%s",
            tool_name,
            action_id,
            action["outcome"] if action else "unknown",
        )
    return ToolResult(content=content, structured_content=envelope)


def transport_payload(result: Any) -> dict[str, Any]:
    """Serialize an MCP CallToolResult into the stable local transport wrapper."""
    content = getattr(result, "content", None) or []
    serialized_content = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(item, "model_dump")
        else item
        for item in content
    ]
    structured = getattr(result, "structured_content", None)
    transport_error: dict[str, Any] | None = None
    if bool(getattr(result, "is_error", False)):
        message = "MCP tool execution failed"
        for item in serialized_content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                message = str(item.get("text") or message)
                break
        transport_error = {
            "code": "TRANSPORT_ERROR",
            "message": message,
            "retry_safety": "reobserve",
        }
        if not isinstance(structured, Mapping):
            structured = None
    return {
        "protocol_version": PROTOCOL_VERSION,
        "result": structured,
        "content": serialized_content,
        "transport_error": transport_error,
    }


def canonical_window(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    window_id = value.get("id")
    pid = value.get("pid")
    if (
        isinstance(window_id, bool)
        or not isinstance(window_id, int)
        or window_id <= 0
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
    ):
        return None
    return {"id": int(window_id), "pid": int(pid)}


def _project_data(
    legacy: MutableMapping[str, Any],
    *,
    success: bool,
    tool_name: str,
) -> dict[str, Any]:
    for key in (
        "success",
        "error",
        "message",
        "suggestion",
        "code",
        "error_code",
        "warnings",
    ):
        legacy.pop(key, None)
    if not success:
        return {}
    if tool_name == "computer_screenshot":
        return {}
    if tool_name == "computer_zoom":
        for key in (
            "format",
            "mime_type",
            "mimeType",
            "width",
            "height",
            "byte_count",
            "imageBytes",
        ):
            legacy.pop(key, None)
    scrubbed = _scrub_public(legacy)
    if isinstance(scrubbed, dict) and set(scrubbed) == {"data"}:
        nested = scrubbed["data"]
        return nested if isinstance(nested, dict) else {"value": nested}
    return scrubbed if isinstance(scrubbed, dict) else {"value": scrubbed}


def _legacy_error_parts(
    legacy: Mapping[str, Any],
) -> tuple[str, str | None, str, dict[str, Any]]:
    raw_error = legacy.get("error")
    if isinstance(raw_error, Mapping):
        message = str(raw_error.get("message") or legacy.get("message") or "Operation failed")
        suggestion = raw_error.get("suggestion") or legacy.get("suggestion")
        source_code = str(
            raw_error.get("code")
            or legacy.get("code")
            or legacy.get("error_code")
            or ""
        )
        details = dict(raw_error.get("details") or {})
    else:
        message = str(raw_error or legacy.get("message") or "Operation failed")
        suggestion = legacy.get("suggestion")
        source_code = str(legacy.get("code") or legacy.get("error_code") or "")
        details = {}
    for key, value in legacy.items():
        if key not in {
            "success",
            "error",
            "message",
            "suggestion",
            "code",
            "error_code",
            "warnings",
        }:
            details[key] = value
    return message, str(suggestion) if suggestion is not None else None, source_code, details


def _normalize_error_code(source_code: str, message: str) -> str:
    normalized = str(source_code or "").strip()
    if normalized in STABLE_ERROR_CODES:
        return normalized
    alias = _ERROR_CODE_ALIASES.get(normalized.casefold())
    if alias:
        return alias

    folded = str(message or "").casefold()
    if "post-action verification failed" in folded or "postcondition" in folded:
        return "POSTCONDITION_FAILED"
    if "permission" in folded or "not allowed" in folded or "disabled by" in folded:
        return "PERMISSION_DENIED"
    if "zoom_id" in folded and ("required" in folded or "requires" in folded):
        return "OBSERVATION_REQUIRED"
    if "snapshot" in folded or "observation" in folded or "screenshot_id" in folded:
        if "stale" in folded or "latest" in folded:
            return "STALE_OBSERVATION"
        if "mismatch" in folded:
            return "OBSERVATION_TARGET_MISMATCH"
        if "required" in folded:
            return "OBSERVATION_REQUIRED"
        if "unknown" in folded or "expired" in folded or "not found" in folded:
            return "OBSERVATION_NOT_FOUND"
    if "window" in folded or "target" in folded:
        if "mismatch" in folded:
            return "TARGET_MISMATCH"
        if "required" in folded or "provide window" in folded:
            return "TARGET_REQUIRED"
        if "not found" in folded or "not currently available" in folded:
            return "TARGET_NOT_FOUND"
    if "uia" in folded or "provider" in folded or "com service" in folded:
        return "PROVIDER_UNAVAILABLE"
    if any(
        marker in folded
        for marker in (
            "must ",
            "requires ",
            " is required",
            " are required",
            "required for ",
            "invalid ",
            "mutually exclusive",
            "missing ",
            "unknown key",
            "unknown action",
            "empty hotkey",
        )
    ):
        return "INVALID_ARGUMENT"
    if "screenshot" in folded or "capture" in folded:
        return "CAPTURE_FAILED"
    if (
        "unsupported" in folded
        or "not supported" in folded
        or "unknown tool" in folded
    ):
        return "UNSUPPORTED_OPERATION"
    return "ACTION_REJECTED"


def _validation_error_message(exc: PydanticValidationError) -> str:
    parts: list[str] = []
    for item in exc.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc") or ())
        error_type = str(item.get("type") or "")
        if error_type == "unexpected_keyword_argument":
            detail = "is not supported by protocol 0.2"
        else:
            detail = str(item.get("msg") or "is invalid")
        parts.append(f"{location}: {detail}" if location else detail)
    return "; ".join(parts) or "Invalid tool arguments"


def _retry_safety_for_error(code: str) -> str:
    if code in {
        "STALE_OBSERVATION",
        "OBSERVATION_NOT_FOUND",
        "OBSERVATION_TARGET_MISMATCH",
        "TARGET_NOT_FOUND",
        "TARGET_MISMATCH",
        "POSTCONDITION_FAILED",
        "ACTION_OUTCOME_UNKNOWN",
        "CAPTURE_FAILED",
        "PROVIDER_UNAVAILABLE",
    }:
        return "reobserve"
    if code in {"PERMISSION_DENIED", "UNSUPPORTED_OPERATION"}:
        return "do_not_retry"
    return "safe"


def _collect_warnings(legacy: MutableMapping[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    raw_warnings = legacy.pop("warnings", [])
    if not isinstance(raw_warnings, list):
        raw_warnings = [raw_warnings]
    for item in raw_warnings:
        warnings.append(_warning_from_item(item))

    capture_after = legacy.get("capture_after")
    if isinstance(capture_after, Mapping) and capture_after.get("success") is False:
        warnings.append(
            {
                "code": "CAPTURE_FAILED",
                "message": str(capture_after.get("error") or "Post-action capture failed"),
                "details": _scrub_public(dict(capture_after)),
            }
        )
        legacy.pop("capture_after", None)

    capture_metadata = legacy.get("capture_metadata")
    if isinstance(capture_metadata, Mapping):
        for item in capture_metadata.get("warnings") or []:
            warnings.append(_warning_from_item(item))
    return warnings


def _warning_from_item(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        code = str(item.get("code") or "WARNING").upper()
        message = str(item.get("message") or code.replace("_", " ").title())
        details = item.get("details")
    else:
        code = str(item or "WARNING").strip().upper().replace(" ", "_")
        message = str(item or "Warning").replace("_", " ")
        details = {}
    return {
        "code": code,
        "message": message,
        "details": _scrub_public(dict(details or {})),
    }


def _extract_legacy_images(
    value: Any,
    content: list[Any],
    artifacts: list[dict[str, Any]],
    *,
    tool_name: str,
    role_hint: str | None = None,
) -> None:
    if isinstance(value, list):
        for item in value:
            _extract_legacy_images(
                item,
                content,
                artifacts,
                tool_name=tool_name,
                role_hint=role_hint,
            )
        return
    if not isinstance(value, MutableMapping):
        return

    screenshots = value.pop("screenshots", None)
    if isinstance(screenshots, list):
        for screenshot in screenshots:
            if not isinstance(screenshot, MutableMapping):
                continue
            role = str(screenshot.get("role") or role_hint or "target")
            image_value = screenshot.pop("screenshot", None)
            path_value = screenshot.pop("path", None)
            _append_legacy_image(
                image_value or path_value,
                screenshot,
                content,
                artifacts,
                tool_name=tool_name,
                role=role,
            )
        value.pop("screenshot", None)

    image_data = value.pop("image_data", None)
    screenshot_value = value.pop("screenshot", None)
    path_value = value.pop("path", None) if _looks_like_capture(value) else None
    if image_data is not None:
        _append_legacy_image(
            image_data,
            value,
            content,
            artifacts,
            tool_name=tool_name,
            role=role_hint or ("zoom" if tool_name == "computer_zoom" else "target"),
            encoded=True,
        )
    elif screenshot_value is not None or path_value is not None:
        _append_legacy_image(
            screenshot_value or path_value,
            value,
            content,
            artifacts,
            tool_name=tool_name,
            role=role_hint or "target",
        )

    for key, item in list(value.items()):
        child_role = "transient" if "transient" in key else role_hint
        _extract_legacy_images(
            item,
            content,
            artifacts,
            tool_name=tool_name,
            role_hint=child_role,
        )


def _append_existing_image_artifact(
    item: Any,
    artifacts: list[dict[str, Any]],
    metadata: Mapping[str, Any],
    *,
    tool_name: str,
    content_index: int,
) -> None:
    encoded = getattr(item, "data", "")
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        raw = b""
    mime_type = str(getattr(item, "mimeType", None) or "image/png")
    width = _optional_int(metadata.get("width"))
    height = _optional_int(metadata.get("height"))
    if width is None or height is None:
        width, height = _image_dimensions(raw)
    artifacts.append(
        _artifact(
            artifact_id=str(
                metadata.get("screenshot_id")
                or metadata.get("zoom_id")
                or f"artifact-{uuid.uuid4().hex}"
            ),
            role="screen" if tool_name == "computer_screenshot" else "target",
            mime_type=mime_type,
            content_index=content_index,
            width=width,
            height=height,
            byte_count=len(raw),
            metadata=metadata,
        )
    )


def _append_legacy_image(
    image_value: Any,
    metadata: Mapping[str, Any],
    content: list[Any],
    artifacts: list[dict[str, Any]],
    *,
    tool_name: str,
    role: str,
    encoded: bool = False,
) -> None:
    raw, fmt = _legacy_image_bytes(image_value, encoded=encoded)
    if not raw:
        return
    width = _optional_int(metadata.get("width") or metadata.get("screen_width"))
    height = _optional_int(metadata.get("height") or metadata.get("screen_height"))
    if width is None or height is None:
        width, height = _image_dimensions(raw)
    mime_type = f"image/{'jpeg' if fmt in {'jpg', 'jpeg'} else fmt}"
    content_index = len(content)
    content.append(Image(data=raw, format=fmt))
    artifacts.append(
        _artifact(
            artifact_id=str(
                metadata.get("screenshot_id")
                or metadata.get("zoom_id")
                or metadata.get("id")
                or f"artifact-{uuid.uuid4().hex}"
            ),
            role=role,
            mime_type=mime_type,
            content_index=content_index,
            width=width,
            height=height,
            byte_count=len(raw),
            metadata=metadata,
        )
    )


def _artifact(
    *,
    artifact_id: str,
    role: str,
    mime_type: str,
    content_index: int,
    width: int | None,
    height: int | None,
    byte_count: int,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": artifact_id,
        "type": "image",
        "role": role,
        "mime_type": mime_type,
        "content_index": int(content_index),
        "width": width,
        "height": height,
        "byte_count": int(byte_count),
    }
    window = canonical_window(metadata.get("window"))
    if window:
        result["window"] = window
    observation_id = metadata.get("observation_id") or metadata.get("snapshot_id")
    if observation_id:
        result["observation_id"] = str(observation_id)
    screenshot_id = metadata.get("screenshot_id") or metadata.get(
        "parent_screenshot_id"
    )
    if screenshot_id:
        result["screenshot_id"] = str(screenshot_id)
    return result


def _append_file_artifacts(
    tool_name: str,
    data: Mapping[str, Any],
    artifacts: list[dict[str, Any]],
) -> None:
    if tool_name not in {"computer_recording", "computer_learn"}:
        return
    path_value = data.get("path") or data.get("output_dir")
    if not isinstance(path_value, str) or not path_value:
        return
    path = Path(path_value)
    artifact_type = "directory" if path.is_dir() or not path.suffix else "file"
    artifacts.append(
        {
            "id": f"artifact-{uuid.uuid4().hex}",
            "type": artifact_type,
            "role": "recording" if tool_name == "computer_recording" else "learning",
            "mime_type": (
                "application/x-directory"
                if artifact_type == "directory"
                else "application/octet-stream"
            ),
            "content_index": None,
            "width": None,
            "height": None,
            "byte_count": path.stat().st_size if path.is_file() else None,
            "path": str(path),
        }
    )


def _legacy_image_bytes(value: Any, *, encoded: bool) -> tuple[bytes, str]:
    if not isinstance(value, str) or not value:
        return b"", "png"
    if encoded:
        try:
            return base64.b64decode(value), "png"
        except Exception:
            return b"", "png"
    if value.startswith("data:image/") and "," in value:
        header, encoded_value = value.split(",", 1)
        fmt = header.split("/", 1)[1].split(";", 1)[0]
        try:
            return base64.b64decode(encoded_value), fmt
        except Exception:
            return b"", fmt
    path = Path(value)
    if path.is_file():
        try:
            raw = path.read_bytes()
        except OSError:
            return b"", "png"
        mime_type, _ = mimetypes.guess_type(path.name)
        fmt = (mime_type or "image/png").split("/", 1)[-1]
        return raw, fmt
    try:
        return base64.b64decode(value, validate=True), "png"
    except Exception:
        return b"", "png"


def _image_dimensions(raw: bytes) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    try:
        from PIL import Image as PILImage

        with PILImage.open(BytesIO(raw)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_like_capture(value: Mapping[str, Any]) -> bool:
    return any(
        key in value
        for key in (
            "screenshot_id",
            "snapshot_id",
            "observation_id",
            "capture_metadata",
            "capture_backend",
            "width",
            "height",
        )
    )


def _scrub_public(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, list):
        return [_scrub_public(item, parent_key=parent_key) for item in value]
    if not isinstance(value, Mapping):
        return value

    if parent_key == "window":
        return canonical_window(value)

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _PUBLIC_REMOVED_KEYS:
            continue
        if key == "snapshot_id":
            result["observation_id"] = _scrub_public(
                item,
                parent_key="observation_id",
            )
            continue
        if key == "snapshot_latest":
            continue
        if key in {"screenshot", "image", "path"} and isinstance(item, str):
            continue
        result[key] = _scrub_public(item, parent_key=key)

    window = canonical_window(result.get("window"))
    if window:
        result["window"] = window
        if result.get("id") == window["id"]:
            result.pop("id", None)
        if result.get("pid") == window.get("pid"):
            result.pop("pid", None)
    else:
        result.pop("window", None)
    return result
