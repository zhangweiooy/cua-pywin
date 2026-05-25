"""Replay recorded trajectories.

Reads turn-NNNNN/ folders from a recording directory and replays each
action via a caller-provided ``tool_executor`` callable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


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
        return {
            "turns_replayed": 0,
            "turns_failed": 0,
            "turns_total": 0,
            "errors": [{"turn": "", "tool": "", "error": f"Directory not found: {recording_dir}"}],
        }

    turn_dirs = sorted(
        d
        for d in os.listdir(recording_dir)
        if d.startswith("turn-") and os.path.isdir(os.path.join(recording_dir, d))
    )

    turns_replayed = 0
    turns_failed = 0
    errors: List[Dict[str, Any]] = []

    for turn_name in turn_dirs:
        turn_path = os.path.join(recording_dir, turn_name)
        action_file = os.path.join(turn_path, "action.json")

        if not os.path.exists(action_file):
            logger.warning("Skipping %s: no action.json", turn_name)
            continue

        try:
            with open(action_file, "r", encoding="utf-8") as f:
                action_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            errors.append({"turn": turn_name, "tool": "", "error": f"Failed to read action.json: {e}"})
            turns_failed += 1
            if stop_on_error:
                break
            continue

        tool_name = action_data.get("tool", "")
        args = action_data.get("args", {})
        window_ref = action_data.get("window_ref")

        logger.info("Replaying %s: %s(%s)", turn_name, tool_name, args)

        try:
            result = await tool_executor(tool_name, args, window_ref=window_ref)
            if isinstance(result, dict) and result.get("success") is False:
                error_msg = result.get("error", "Unknown error")
                errors.append({"turn": turn_name, "tool": tool_name, "error": error_msg})
                turns_failed += 1
                if stop_on_error:
                    break
            else:
                turns_replayed += 1
        except Exception as e:
            errors.append({"turn": turn_name, "tool": tool_name, "error": str(e)})
            turns_failed += 1
            if stop_on_error:
                break

        # Delay between actions
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    return {
        "turns_replayed": turns_replayed,
        "turns_failed": turns_failed,
        "turns_total": len(turn_dirs),
        "completed": turns_failed == 0,
        "errors": errors,
    }
