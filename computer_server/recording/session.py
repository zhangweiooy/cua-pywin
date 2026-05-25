"""Recording session management.

Records MCP tool invocations to numbered turn folders containing:
- action.json: tool name, args, result, timestamp
- screenshot.png: screen state after action (if provided)
- click.png: screenshot with red dot at click point (if applicable)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tools that should be recorded (action tools)
RECORDABLE_TOOLS = frozenset(
    [
        "computer_click",
        "computer_drag",
        "computer_type",
        "computer_press_key",
        "computer_hotkey",
        "computer_key_down",
        "computer_key_up",
        "computer_set_value",
        "computer_page",
        "computer_open",
        "computer_scroll",
        "computer_launch_app",
        "computer_activate_window",
    ]
)


class RecordingSession:
    """Records tool invocations to turn folders."""

    def __init__(self, output_dir: str, video: bool = False):
        self.output_dir = output_dir
        self.video = video
        self.turn_counter = 0
        self.active = False
        self._start_time: Optional[float] = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        """Start recording session."""
        os.makedirs(self.output_dir, exist_ok=True)
        self.active = True
        self.turn_counter = 0
        self._start_time = time.time()
        logger.info("Recording started: %s", self.output_dir)

    def stop(self) -> Dict[str, Any]:
        """Stop recording session and return summary."""
        self.active = False
        duration = time.time() - self._start_time if self._start_time else 0
        summary = {
            "turns": self.turn_counter,
            "duration_seconds": round(duration, 2),
            "path": self.output_dir,
        }
        logger.info("Recording stopped: %d turns, %.1fs", self.turn_counter, duration)
        return summary

    @property
    def state(self) -> Dict[str, Any]:
        """Get current recording state."""
        duration = time.time() - self._start_time if self._start_time else 0
        return {
            "active": self.active,
            "turn_count": self.turn_counter,
            "output_dir": self.output_dir,
            "duration_seconds": round(duration, 2),
        }

    async def record_turn(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        screenshot_bytes: Optional[bytes] = None,
        click_point: Optional[Tuple[int, int]] = None,
        window_ref: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a single tool invocation as a turn.

        Creates turn-NNNNN/ folder with:
        - action.json: tool, args, result, timestamp, turn number
        - screenshot.png: screen state after action (if provided)
        - click.png: screenshot with red dot at click point (if applicable)
        """
        if not self.active:
            return
        if tool_name not in RECORDABLE_TOOLS:
            return

        async with self._lock:
            self.turn_counter += 1
            turn_num = self.turn_counter
            turn_dir = os.path.join(self.output_dir, f"turn-{turn_num:05d}")

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._write_turn,
                turn_dir,
                tool_name,
                args,
                result,
                screenshot_bytes,
                click_point,
                window_ref,
                turn_num,
            )

    def _write_turn(
        self,
        turn_dir: str,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        screenshot_bytes: Optional[bytes],
        click_point: Optional[Tuple[int, int]],
        window_ref: Optional[Dict[str, Any]],
        turn_number: int,
    ) -> None:
        """Synchronous file writing (runs in executor)."""
        os.makedirs(turn_dir, exist_ok=True)

        # Write action.json
        action_data = {
            "tool": tool_name,
            "args": self._sanitize_for_json(args),
            "result": self._sanitize_for_json(result),
            "timestamp": time.time(),
            "turn_number": turn_number,
        }
        if window_ref:
            action_data["window_ref"] = window_ref
        action_path = os.path.join(turn_dir, "action.json")
        with open(action_path, "w", encoding="utf-8") as f:
            json.dump(action_data, f, indent=2, ensure_ascii=False, default=str)

        # Write screenshot if provided
        if screenshot_bytes:
            screenshot_path = os.path.join(turn_dir, "screenshot.png")
            with open(screenshot_path, "wb") as f:
                f.write(screenshot_bytes)

            # If click action, draw red dot on screenshot
            if click_point:
                try:
                    from PIL import Image as PILImage
                    from PIL import ImageDraw
                    import io

                    img = PILImage.open(io.BytesIO(screenshot_bytes))
                    draw = ImageDraw.Draw(img)
                    cx, cy = click_point
                    radius = 8
                    draw.ellipse(
                        [(cx - radius, cy - radius), (cx + radius, cy + radius)],
                        fill="red",
                        outline="darkred",
                        width=2,
                    )
                    click_path = os.path.join(turn_dir, "click.png")
                    img.save(click_path, format="PNG")
                except Exception as e:
                    logger.warning("Failed to create click image: %s", e)

    @staticmethod
    def _sanitize_for_json(data: Any) -> Any:
        """Remove non-serializable fields (bytes, platform refs, base64 blobs)."""
        if isinstance(data, dict):
            return {
                k: RecordingSession._sanitize_for_json(v)
                for k, v in data.items()
                if not isinstance(v, (bytes, bytearray))
                and k
                not in (
                    "base64_image",
                    "screenshot",
                    "image",
                    "image_data",
                    "platform_ref",
                    "element_ref",
                )
            }
        elif isinstance(data, (list, tuple)):
            return [RecordingSession._sanitize_for_json(item) for item in data]
        elif isinstance(data, (str, int, float, bool, type(None))):
            return data
        else:
            return str(data)


# Global recording session (module-level singleton)
_recording_session: Optional[RecordingSession] = None


def get_recording_session() -> Optional[RecordingSession]:
    """Get the current recording session (None if not started)."""
    return _recording_session


def set_recording_session(session: Optional[RecordingSession]) -> None:
    """Set the global recording session."""
    global _recording_session
    _recording_session = session
