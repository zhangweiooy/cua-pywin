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
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tools that should be recorded (action tools)
RECORDABLE_TOOLS = frozenset(
    [
        "computer_click",
        "computer_move",
        "computer_drag",
        "computer_mouse_down",
        "computer_mouse_up",
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

# Sentinel value to signal the writer thread to stop.
_WRITER_STOP = object()
# Bounded queue capacity to prevent unbounded memory growth.
_MAX_QUEUE_SIZE = 64


@dataclass(frozen=True)
class RecordedTurnContext:
    """Event-loop side payload for recording a tool invocation."""

    tool_name: str
    args: Dict[str, Any]
    result: Dict[str, Any]
    click_point: Optional[Tuple[int, int]] = None
    window_ref: Optional[Dict[str, Any]] = None
    element_ref: Optional[Dict[str, Any]] = None


class RecordingSession:
    """Records tool invocations to turn folders.

    Uses a dedicated daemon writer thread with a bounded queue so that
    turn writes never compete with the default executor (UIA / SOM /
    screenshot).  record_turn remains async but only allocates a turn
    number under asyncio.Lock and enqueues an immutable payload; the
    writer thread handles all disk I/O.
    """

    def __init__(
        self,
        output_dir: str,
        video: bool = False,
        capture_screenshots: bool = True,
        screenshot_every_n: int = 1,
    ):
        self.output_dir = output_dir
        self.video = video
        self.capture_screenshots = capture_screenshots
        self.screenshot_every_n = max(1, int(screenshot_every_n))
        self.turn_counter = 0
        self.active = False
        self.dropped_turns = 0
        self.write_errors = 0
        self.screenshot_errors = 0
        self._start_time: Optional[float] = None
        self._lock = asyncio.Lock()
        # Dedicated writer thread + bounded queue
        self._writer_queue: Optional[queue.Queue] = None
        self._writer_thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def start(self) -> None:
        """Start recording session."""
        os.makedirs(self.output_dir, exist_ok=True)
        self.active = True
        self.turn_counter = 0
        self._start_time = time.time()
        self._start_writer()
        logger.info("Recording started: %s", self.output_dir)

    def stop(self) -> Dict[str, Any]:
        """Stop recording session and return summary.

        Sets active=False first (prevents new enqueues), then sends the
        stop sentinel, then joins the writer thread with a short
        timeout so the caller can safely set_recording_session(None)
        afterwards.
        """
        self.active = False
        duration = time.time() - self._start_time if self._start_time else 0

        # Flush the writer queue.
        # Best-effort delivery of _WRITER_STOP: if the queue is full we
        # drain pending turns (discarding them) so the sentinel always
        # reaches the writer thread.  Without the sentinel the daemon
        # writer thread blocks forever on q.get() and leaks until the
        # process exits.
        if self._writer_queue is not None:
            # 1) Quick path: try to put with a short timeout.
            try:
                self._writer_queue.put(_WRITER_STOP, timeout=0.5)
            except queue.Full:
                # 2) Drain pending turns to free at least one slot,
                #    then deliver the sentinel.  Hard deadline so the
                #    caller is never blocked indefinitely.
                logger.warning(
                    "Recording writer queue full at shutdown; "
                    "draining pending turns to deliver stop sentinel"
                )
                drained = 0
                deadline = time.monotonic() + 2.0
                sentinel_delivered = False
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # Last-resort attempt without waiting.
                        try:
                            self._writer_queue.put_nowait(_WRITER_STOP)
                            sentinel_delivered = True
                        except queue.Full:
                            logger.error(
                                "Failed to deliver stop sentinel "
                                "after draining %d turn(s); writer "
                                "thread will leak until process exit",
                                drained,
                            )
                        break
                    try:
                        self._writer_queue.get_nowait()
                        self._writer_queue.task_done()
                        drained += 1
                    except queue.Empty:
                        # Queue emptied — safe to deliver sentinel.
                        self._writer_queue.put_nowait(_WRITER_STOP)
                        sentinel_delivered = True
                        break
                if drained:
                    logger.warning(
                        "Dropped %d pending turn(s) during shutdown",
                        drained,
                    )
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=5.0)
            self._writer_queue = None
            self._writer_thread = None

        summary = {
            "turns": self.turn_counter,
            "dropped_turns": self.dropped_turns,
            "write_errors": self.write_errors,
            "screenshot_errors": self.screenshot_errors,
            "duration_seconds": round(duration, 2),
            "path": self.output_dir,
        }
        logger.info("Recording stopped: %d turns, %.1fs", self.turn_counter, duration)
        return summary

    # ----------------------------------------------------------------
    # State query
    # ----------------------------------------------------------------

    @property
    def state(self) -> Dict[str, Any]:
        """Get current recording state."""
        duration = time.time() - self._start_time if self._start_time else 0
        return {
            "active": self.active,
            "turn_count": self.turn_counter,
            "output_dir": self.output_dir,
            "duration_seconds": round(duration, 2),
            "capture_screenshots": self.capture_screenshots,
            "screenshot_every_n": self.screenshot_every_n,
            "dropped_turns": self.dropped_turns,
            "write_errors": self.write_errors,
            "screenshot_errors": self.screenshot_errors,
        }

    def should_capture_screenshot(self) -> bool:
        """Return whether the next turn should include screenshot bytes."""
        if not self.capture_screenshots:
            return False
        next_turn = self.turn_counter + 1
        return next_turn % self.screenshot_every_n == 0

    def note_screenshot_error(self) -> None:
        self.screenshot_errors += 1

    # ----------------------------------------------------------------
    # Async turn recording (event-loop side)
    # ----------------------------------------------------------------

    async def record_turn(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        screenshot_bytes: Optional[bytes] = None,
        click_point: Optional[Tuple[int, int]] = None,
        window_ref: Optional[Dict[str, Any]] = None,
        element_ref: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a single tool invocation as a turn.

        Creates turn-NNNNN/ folder with:
        - action.json: tool, args, result, timestamp, turn number
        - screenshot.png: screen state after action (if provided)
        - click.png: screenshot with red dot at click point (if applicable)

        This method is async but *never* blocks on I/O.  It only
        allocates a turn number under asyncio.Lock and enqueues an
        immutable payload into the writer queue.  If the queue is full
        a warning is logged and the turn is dropped (no exception).
        """
        if not self.active:
            return
        if tool_name not in RECORDABLE_TOOLS:
            return

        async with self._lock:
            # Re-check active under lock to avoid racing with stop().
            if not self.active:
                return
            self.turn_counter += 1
            turn_num = self.turn_counter
            turn_dir = os.path.join(self.output_dir, f"turn-{turn_num:05d}")

            # Build an immutable-ish payload tuple for the writer thread.
            # All items are immutable (int, str, bytes, None, tuples of ints,
            # dicts that won't be mutated later) so the writer thread can
            # consume them without any locking.
            payload = (
                turn_dir,
                tool_name,
                args,
                result,
                screenshot_bytes,
                click_point,
                window_ref,
                element_ref,
                turn_num,
            )

            if self._writer_queue is not None:
                try:
                    self._writer_queue.put_nowait(payload)
                except queue.Full:
                    self.dropped_turns += 1
                    logger.warning(
                        "Recording writer queue full (maxsize=%d), "
                        "dropping turn-%05d (%s)",
                        _MAX_QUEUE_SIZE,
                        turn_num,
                        tool_name,
                    )
            else:
                logger.warning(
                    "Recording writer queue not initialized, "
                    "dropping turn-%05d",
                    turn_num,
                )

    # ----------------------------------------------------------------
    # Writer thread (dedicated, no event-loop contention)
    # ----------------------------------------------------------------

    def _start_writer(self) -> None:
        """Create the bounded queue and spawn the daemon writer thread."""
        self._writer_queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="recording-writer",
        )
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        """Run in the dedicated writer thread.

        Blocks on queue.get(), writes each turn synchronously, and
        exits cleanly when it receives the _WRITER_STOP sentinel.
        Exceptions from _write_turn are caught and logged so a single
        bad turn does not kill the thread.
        """
        q = self._writer_queue
        while True:
            item = q.get()
            if item is _WRITER_STOP:
                q.task_done()
                break
            try:
                (
                    turn_dir,
                    tool_name,
                    args,
                    result,
                    screenshot_bytes,
                    click_point,
                    window_ref,
                    element_ref,
                    turn_num,
                ) = item
                self._write_turn(
                    turn_dir,
                    tool_name,
                    args,
                    result,
                    screenshot_bytes,
                    click_point,
                    window_ref,
                    element_ref,
                    turn_num,
                )
            except Exception:
                self.write_errors += 1
                logger.exception(
                    "Writer thread error for turn-%05d", item[-1]
                )
            finally:
                q.task_done()

    # ----------------------------------------------------------------
    # Synchronous file writing (called from writer thread only)
    # ----------------------------------------------------------------

    def _write_turn(
        self,
        turn_dir: str,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        screenshot_bytes: Optional[bytes],
        click_point: Optional[Tuple[int, int]],
        window_ref: Optional[Dict[str, Any]],
        element_ref: Optional[Dict[str, Any]],
        turn_number: int,
    ) -> None:
        """Synchronous file writing (runs in writer thread)."""
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
        if element_ref:
            action_data["element_ref"] = self._sanitize_for_json(element_ref)
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
