"""Replay recorded trajectories.

Reads turn-NNNNN/ folders from a recording directory and replays each
action via a caller-provided ``tool_executor`` callable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Callable, Dict, List

from computer_server.recording.session import (
    RECORDING_MANIFEST_NAME,
    RECORDING_PROTOCOL_VERSION,
)

logger = logging.getLogger(__name__)

_PREFLIGHT_FAILURE_CODES = frozenset(
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
        "ACTION_REJECTED",
    }
)


async def replay_trajectory(
    recording_dir: str,
    tool_executor: Callable[..., Any],
    delay_ms: int = 500,
    stop_on_error: bool = True,
) -> Dict[str, Any]:
    """Replay a recorded trajectory.

    Args:
        recording_dir: Path to recording directory (containing turn-NNNNN folders).
        tool_executor: Async callable that takes (tool_name, args) and returns result dict.
        delay_ms: Delay between actions in milliseconds.
        stop_on_error: Stop on first failed action.

    Returns:
        {turns_replayed, turns_failed, turns_total, errors: [{turn, tool, error}]}
    """
    if not os.path.isdir(recording_dir):
        error_message = f"Directory not found: {recording_dir}"
        return {
            "turns_replayed": 0,
            "turns_failed": 0,
            "turns_total": 0,
            "completed": False,
            "code": "INVALID_ARGUMENT",
            "error": error_message,
            "errors": [{"turn": "", "tool": "", "error": error_message}],
        }

    manifest_path = os.path.join(recording_dir, RECORDING_MANIFEST_NAME)
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        error_message = f"Recording is not a cua-win 0.2 trajectory: {exc}"
        return {
            "turns_replayed": 0,
            "turns_failed": 0,
            "turns_total": 0,
            "completed": False,
            "code": "INVALID_ARGUMENT",
            "error": error_message,
            "errors": [
                {
                    "turn": "",
                    "tool": "",
                    "error": error_message,
                }
            ],
        }
    if not isinstance(manifest, Mapping):
        error_message = "Recording manifest must be a JSON object"
        return {
            "turns_replayed": 0,
            "turns_failed": 0,
            "turns_total": 0,
            "completed": False,
            "code": "INVALID_ARGUMENT",
            "error": error_message,
            "errors": [
                {
                    "turn": "",
                    "tool": "",
                    "error": error_message,
                }
            ],
        }
    if manifest.get("protocol_version") != RECORDING_PROTOCOL_VERSION:
        error_message = (
            "Unsupported recording protocol_version "
            f"{manifest.get('protocol_version')!r}; expected "
            f"{RECORDING_PROTOCOL_VERSION!r}"
        )
        return {
            "turns_replayed": 0,
            "turns_failed": 0,
            "turns_total": 0,
            "completed": False,
            "code": "UNSUPPORTED_OPERATION",
            "error": error_message,
            "errors": [
                {
                    "turn": "",
                    "tool": "",
                    "error": error_message,
                }
            ],
        }

    turn_dirs = sorted(
        d
        for d in os.listdir(recording_dir)
        if d.startswith("turn-") and os.path.isdir(os.path.join(recording_dir, d))
    )

    turns_replayed = 0
    turns_failed = 0
    outcome_unknown = False
    first_failure_code: str | None = None
    errors: List[Dict[str, Any]] = []

    for turn_name in turn_dirs:
        turn_path = os.path.join(recording_dir, turn_name)
        action_file = os.path.join(turn_path, "action.json")

        if not os.path.exists(action_file):
            errors.append(
                {
                    "turn": turn_name,
                    "tool": "",
                    "error": "Missing action.json",
                }
            )
            turns_failed += 1
            first_failure_code = first_failure_code or "INVALID_ARGUMENT"
            if stop_on_error:
                break
            continue

        try:
            with open(action_file, "r", encoding="utf-8") as f:
                action_data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeError) as e:
            errors.append({"turn": turn_name, "tool": "", "error": f"Failed to read action.json: {e}"})
            turns_failed += 1
            first_failure_code = first_failure_code or "INVALID_ARGUMENT"
            if stop_on_error:
                break
            continue

        if not isinstance(action_data, Mapping):
            errors.append(
                {
                    "turn": turn_name,
                    "tool": "",
                    "error": "action.json must contain a JSON object",
                }
            )
            turns_failed += 1
            first_failure_code = first_failure_code or "INVALID_ARGUMENT"
            if stop_on_error:
                break
            continue

        turn_protocol_version = action_data.get("protocol_version")
        if turn_protocol_version != RECORDING_PROTOCOL_VERSION:
            errors.append(
                {
                    "turn": turn_name,
                    "tool": str(action_data.get("tool") or ""),
                    "error": (
                        f"Unsupported turn protocol_version "
                        f"{turn_protocol_version!r}; expected "
                        f"{RECORDING_PROTOCOL_VERSION!r}"
                    ),
                }
            )
            turns_failed += 1
            first_failure_code = first_failure_code or "UNSUPPORTED_OPERATION"
            if stop_on_error:
                break
            continue

        tool_name = action_data.get("tool", "")
        args = action_data.get("args", {})
        window_ref = action_data.get("window_ref")
        element_ref = action_data.get("element_ref")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(args, Mapping)
            or (window_ref is not None and not isinstance(window_ref, Mapping))
            or (element_ref is not None and not isinstance(element_ref, Mapping))
        ):
            errors.append(
                {
                    "turn": turn_name,
                    "tool": tool_name if isinstance(tool_name, str) else "",
                    "error": "action.json has an invalid tool, args, or replay reference",
                }
            )
            turns_failed += 1
            first_failure_code = first_failure_code or "INVALID_ARGUMENT"
            if stop_on_error:
                break
            continue

        logger.info("Replaying %s: %s(%s)", turn_name, tool_name, args)

        try:
            result = await tool_executor(
                tool_name,
                args,
                window_ref=dict(window_ref) if window_ref is not None else None,
                element_ref=dict(element_ref) if element_ref is not None else None,
            )
            if isinstance(result, dict) and result.get("success") is False:
                error_msg = result.get("error", "Unknown error")
                errors.append({"turn": turn_name, "tool": tool_name, "error": error_msg})
                turns_failed += 1
                result_code = str(result.get("code") or "")
                first_failure_code = first_failure_code or result_code or "ACTION_REJECTED"
                if result_code not in _PREFLIGHT_FAILURE_CODES:
                    outcome_unknown = True
                if stop_on_error:
                    break
            else:
                turns_replayed += 1
        except Exception as e:
            errors.append({"turn": turn_name, "tool": tool_name, "error": str(e)})
            turns_failed += 1
            first_failure_code = first_failure_code or "ACTION_OUTCOME_UNKNOWN"
            outcome_unknown = True
            if stop_on_error:
                break

        # Delay between actions
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    replay_result = {
        "turns_replayed": turns_replayed,
        "turns_failed": turns_failed,
        "turns_total": len(turn_dirs),
        "completed": turns_failed == 0,
        "errors": errors,
    }
    if errors:
        replay_result["error"] = str(errors[0]["error"])
    if turns_failed and (turns_replayed or outcome_unknown):
        replay_result.update(
            {
                "code": "ACTION_OUTCOME_UNKNOWN",
                "suggestion": (
                    "Reobserve the target before deciding whether to replay again."
                ),
            }
        )
    elif turns_failed:
        replay_result["code"] = first_failure_code or "INVALID_ARGUMENT"
    return replay_result
