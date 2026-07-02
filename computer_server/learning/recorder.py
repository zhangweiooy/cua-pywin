"""LearningRecorder — core recording engine (platform-aware, thread-safe).

Mirrors Ghost OS Swift LearningRecorder:
- Owns the session state behind threading.Lock
- Delegates platform input capture to platform/
- Orchestrates keystroke/scroll coalescing
- Handles app switch detection and security field filtering
- Enriches mouse clicks with UIA element metadata in real-time

Thread safety: the lock is only held briefly to check/update session state.
Heavy work (UIA calls, coalescer flushes, app detection) happens outside
the lock, matching the Swift CGEvent callback pattern.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .coalescer import KeystrokeBuncher, ScrollBuncher
from .platform import (
    BROWSER_PROCESSES,
    KEY_CODE_TO_NAME,
    MAX_RECORDING_DURATION,
    RESTRICTED_PROCESSES,
    SENSITIVE_FIELD_PATTERNS,
)
from .session import (
    ElementContext,
    LearningError,
    LearningLink,
    LearningObservation,
    LearningSession,
    ObservedAction,
    ObservedActionType,
)

logger = logging.getLogger(__name__)

OBSERVATION_QUEUE_MAXSIZE = 128
OBSERVATION_STOP_TIMEOUT_SECONDS = 2.0


@dataclass
class _ObservationJob:
    trigger: str
    timestamp: int
    correlation_id: str | None
    action_id: str | None = None
    app_name: str | None = None
    app_bundle_id: str | None = None
    window_title: str | None = None
    url: str | None = None
    element_context: ElementContext | None = None
    x: float | None = None
    y: float | None = None


class LearningRecorder:
    """Records user input events (keyboard, mouse, scroll) into a LearningSession.

    Thread-safe. All public methods acquire the lock briefly.
    Input events arrive on pynput listener threads.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._session: Optional[LearningSession] = None
        self._input: Optional[object] = None  # InputCapture
        self._uia: Optional[object] = None  # UIAEnricher
        self._app_detector: Optional[object] = None  # AppDetector

        # Coalescers — created when recording starts
        self._keystroke_buncher: Optional[KeystrokeBuncher] = None
        self._scroll_buncher: Optional[ScrollBuncher] = None

        self._output_dir: str = ""
        self._last_click_x: float = 0
        self._last_click_y: float = 0
        self._last_uia_value: Optional[str] = None

        # Max duration safety timer
        self._max_duration_timer: Optional[threading.Timer] = None

        # Prevent concurrent stop() from timer + user call
        self._stopping = False
        self._agent_input_suppression_count = 0
        self._agent_input_suppressed_until = 0.0
        self._observation_queue: queue.Queue[_ObservationJob | None] | None = None
        self._observation_thread: threading.Thread | None = None
        self._observation_stop = threading.Event()

        from computer_server.services.uia_service import UIACallGate

        self._uia_gate = UIACallGate()

    # ---- lazy platform init ----

    def _init_platform(self) -> None:
        """Lazy-import platform modules so the package is importable
        on any OS for testing purposes."""
        from .platform import AppDetector, InputCapture, UIAEnricher
        self._input_cls = InputCapture
        self._uia_cls = UIAEnricher
        self._app_detector_cls = AppDetector

    # ================================================================
    # Public API
    # ================================================================

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._session is not None

    def start(self, task_description: Optional[str] = None,
              output_dir: str = "") -> Optional[LearningError]:
        """Start recording user input.

        Returns None on success, or a LearningError.
        """
        with self._lock:
            if self._session is not None:
                return LearningError.ALREADY_RECORDING

            self._stopping = False
            self._output_dir = output_dir
            self._init_platform()

            # Pre-warm OCR on main thread so enrichment daemon doesn't block on import
            try:
                from .platform import VisionEnricher
                _ve = VisionEnricher()
                _ve._ensure_ocr()
            except Exception:
                pass

            self._session = LearningSession(
                task_description=task_description,
                start_time=time.time(),
            )
            self._app_detector = self._app_detector_cls()

            # Create coalescers with flush callbacks
            self._keystroke_buncher = KeystrokeBuncher(
                flush_callback=self._on_keystroke_flush
            )
            self._scroll_buncher = ScrollBuncher(
                flush_callback=self._on_scroll_flush
            )

            self._uia = self._uia_cls()
            if not self._uia.health_check():
                self._session = None
                self._cleanup()
                return LearningError.UIA_UNAVAILABLE
            self._start_observation_worker()

            # Start input capture
            self._input = self._input_cls()
            try:
                self._input.start(
                    on_key_down=self._on_key_down,
                    on_mouse_down=self._on_mouse_down,
                    on_scroll=self._on_scroll,
                )
            except Exception as e:
                logger.error("Failed to start input capture: %s", e)
                self._session = None
                if self._input:
                    try:
                        self._input.stop()
                    except Exception:
                        pass
                self._cleanup()
                return LearningError.HOOK_CREATION_FAILED

            # Start max duration safety timer
            self._max_duration_timer = threading.Timer(
                MAX_RECORDING_DURATION,
                self._on_max_duration,
            )
            self._max_duration_timer.daemon = True
            self._max_duration_timer.start()

            logger.info("Learning: recording started (%s)", task_description or "no description")
            return None

    def stop(self) -> tuple:
        """Stop recording and return (is_success, result_or_error).

        Returns:
            On success: (True, (LearningSession, list[ObservedAction]))
            On failure: (False, LearningError)
        """
        with self._lock:
            if self._stopping:
                return (False, LearningError.NOT_RECORDING)
            self._stopping = True

        return self._finish_stop()

    def status(self) -> tuple:
        """Return (is_recording, action_count, duration_seconds, current_app, listener_healthy)."""
        with self._lock:
            if self._session is None:
                return (False, 0, 0.0, None, None)
            duration = time.time() - self._session.start_time
            count = len(self._session.actions)
            app = self._app_detector.current_app_info()[0] if self._app_detector else None
            healthy = self._input.is_healthy() if self._input else None
            return (True, count, duration, app, healthy)

    def status_dict(self) -> dict[str, Any]:
        """Return additive learning status details for MCP/CLI callers."""
        with self._lock:
            if self._session is None:
                return self._inactive_status_dict()

            duration = time.time() - self._session.start_time
            app = self._app_detector.current_app_info()[0] if self._app_detector else None
            healthy = self._input.is_healthy() if self._input else None
            metrics = self._session_metrics_snapshot()
            mode = "full" if healthy is True and self._observation_thread_alive() else "reduced"
            data: dict[str, Any] = {
                "recording": True,
                "mode": mode,
                "action_count": len(self._session.actions),
                "observation_count": len(self._session.observations),
                "pending_observations": metrics["pending_observations"],
                "dropped_observations": metrics["dropped_observations"],
                "timeout_count": metrics["timeout_count"],
                "sensitive_skip_count": metrics["sensitive_skip_count"],
                "last_event_at": metrics["last_event_at"],
                "last_observation_at": metrics["last_observation_at"],
                "duration_seconds": int(duration),
            }
            if app:
                data["current_app"] = app
            if healthy is not None:
                data["listener_healthy"] = healthy
            return data

    def suppress_agent_input(self, duration_seconds: float = 1.0) -> None:
        """Temporarily ignore events likely injected by agent tools."""
        self.flush_pending_input()
        duration_seconds = max(0.0, float(duration_seconds))
        with self._lock:
            self._agent_input_suppressed_until = max(
                self._agent_input_suppressed_until,
                time.monotonic() + duration_seconds,
            )

    def begin_agent_input_suppression(self) -> None:
        """Start a scoped suppression span for held-key/button primitives."""
        self.flush_pending_input()
        with self._lock:
            self._agent_input_suppression_count += 1

    def end_agent_input_suppression(self, grace_seconds: float = 0.25) -> None:
        """End a scoped suppression span and keep a short event-settle grace."""
        with self._lock:
            if self._agent_input_suppression_count > 0:
                self._agent_input_suppression_count -= 1
            self._agent_input_suppressed_until = max(
                self._agent_input_suppressed_until,
                time.monotonic() + max(0.0, float(grace_seconds)),
            )

    def clear_agent_input_suppression(self) -> None:
        """Clear all scoped suppression state."""
        with self._lock:
            self._agent_input_suppression_count = 0
            self._agent_input_suppressed_until = 0.0

    def flush_pending_input(self) -> None:
        """Flush coalescers so user input is separated from agent input."""
        if self._keystroke_buncher:
            self._keystroke_buncher.flush()
        if self._scroll_buncher:
            self._scroll_buncher.flush()

    @staticmethod
    def _inactive_status_dict() -> dict[str, Any]:
        return {
            "recording": False,
            "mode": "off",
            "action_count": 0,
            "observation_count": 0,
            "pending_observations": 0,
            "dropped_observations": 0,
            "timeout_count": 0,
            "sensitive_skip_count": 0,
            "last_event_at": None,
            "last_observation_at": None,
            "duration_seconds": 0,
        }

    def _session_metrics_snapshot(self) -> dict[str, Any]:
        if self._session is None:
            return self._inactive_status_dict()
        metrics = dict(self._session.metrics)
        metrics.setdefault("pending_observations", 0)
        metrics.setdefault("dropped_observations", 0)
        metrics.setdefault("timeout_count", 0)
        metrics.setdefault("sensitive_skip_count", 0)
        metrics.setdefault("last_event_at", None)
        metrics.setdefault("last_observation_at", None)
        return metrics

    def _observation_thread_alive(self) -> bool:
        return self._observation_thread is not None and self._observation_thread.is_alive()

    def _cleanup(self):
        self._stop_observation_worker()
        self._input = None
        self._uia = None
        self._app_detector = None
        self._keystroke_buncher = None
        self._scroll_buncher = None
        self._last_uia_value = None
        self._agent_input_suppression_count = 0
        self._agent_input_suppressed_until = 0.0

    # ================================================================
    # Observation worker
    # ================================================================

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def _start_observation_worker(self) -> None:
        self._stop_observation_worker()
        self._observation_stop.clear()
        self._observation_queue = queue.Queue(maxsize=OBSERVATION_QUEUE_MAXSIZE)
        self._observation_thread = threading.Thread(
            target=self._observation_loop,
            name="cua-learn-observation",
            daemon=True,
        )
        self._observation_thread.start()

    def _stop_observation_worker(self) -> None:
        thread = self._observation_thread
        q = self._observation_queue
        if thread is None and q is None:
            return
        self._observation_stop.set()
        if q is not None:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=OBSERVATION_STOP_TIMEOUT_SECONDS)
        self._observation_thread = None
        self._observation_queue = None
        self._observation_stop.clear()

    def _observation_loop(self) -> None:
        while not self._observation_stop.is_set():
            q = self._observation_queue
            if q is None:
                return
            try:
                job = q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if job is None:
                    return
                observation = self._build_observation(job)
                self._complete_observation(job, observation)
            except Exception as exc:
                logger.debug("Learning observation failed", exc_info=True)
                failed = LearningObservation(
                    observation_id=self._new_id("obs"),
                    correlation_id=job.correlation_id if job is not None else None,
                    trigger=job.trigger if job is not None else "unknown",
                    timestamp=time.perf_counter_ns(),
                    error=str(exc),
                    capture_backend="none",
                    tree_quality="empty",
                )
                if job is not None:
                    self._complete_observation(job, failed)
            finally:
                q.task_done()

    def _queue_observation(self, job: _ObservationJob) -> None:
        q = self._observation_queue
        if q is None:
            return
        with self._lock:
            if self._session is None:
                return
            self._session.metrics["pending_observations"] += 1
        try:
            q.put_nowait(job)
        except queue.Full:
            with self._lock:
                if self._session is not None:
                    self._session.metrics["pending_observations"] = max(
                        0,
                        self._session.metrics["pending_observations"] - 1,
                    )
                    self._session.metrics["dropped_observations"] += 1

    def _queue_observation_for_action(self, action: ObservedAction, trigger: str) -> None:
        self._queue_observation(
            _ObservationJob(
                trigger=trigger,
                timestamp=time.perf_counter_ns(),
                correlation_id=action.correlation_id,
                action_id=action.action_id,
                app_name=action.app_name,
                app_bundle_id=action.app_bundle_id,
                window_title=action.window_title,
                url=action.url,
                element_context=action.element_context,
                x=action.x,
                y=action.y,
            )
        )

    def _queue_manual_stop_observation(self) -> None:
        with self._lock:
            if self._session is None or not self._session.actions:
                return
            app_detector = self._app_detector
        app_name = None
        app_bundle_id = None
        window_title = None
        if app_detector is not None:
            try:
                app_name, app_bundle_id, window_title = app_detector.current_app_info()
            except Exception:
                logger.debug("Failed to read app info for stop observation", exc_info=True)
        self._queue_observation(
            _ObservationJob(
                trigger="manual_stop_flush",
                timestamp=time.perf_counter_ns(),
                correlation_id=self._new_id("corr"),
                app_name=app_name,
                app_bundle_id=app_bundle_id,
                window_title=window_title,
            )
        )

    def _wait_for_observation_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                pending = (
                    self._session.metrics["pending_observations"]
                    if self._session is not None
                    else 0
                )
            if pending <= 0:
                return True
            time.sleep(0.02)
        with self._lock:
            if self._session is not None:
                pending = self._session.metrics["pending_observations"]
                if pending > 0:
                    self._session.metrics["dropped_observations"] += pending
                    self._session.metrics["pending_observations"] = 0
        return False

    def _build_observation(self, job: _ObservationJob) -> LearningObservation:
        started = time.perf_counter()
        app_name = job.app_name
        app_bundle_id = job.app_bundle_id
        window_title = job.window_title
        url = job.url
        element_context = job.element_context

        if self._is_sensitive_context(self._uia, element_context):
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return LearningObservation(
                observation_id=self._new_id("obs"),
                correlation_id=job.correlation_id,
                trigger=job.trigger,
                timestamp=time.perf_counter_ns(),
                app_name=app_name,
                app_bundle_id=app_bundle_id,
                window_title=window_title,
                skip_reason="sensitive_context",
                capture_backend="none",
                tree_quality="empty",
                timing={"total_ms": elapsed_ms},
            )

        if self._app_detector is not None and (not app_name or not window_title):
            try:
                app_name, app_bundle_id, window_title = self._app_detector.current_app_info()
            except Exception:
                logger.debug("Observation app detection failed", exc_info=True)

        if url is None and app_bundle_id:
            url = self._uia_gate.run_with_timeout(
                lambda: self._get_current_url(app_bundle_id),
                timeout=0.5,
            )

        if element_context is None and self._uia is not None:
            if job.x is not None and job.y is not None:
                ctx = self._uia_gate.run_with_timeout(
                    lambda: self._uia.enrich_click(job.x, job.y),
                    timeout=0.75,
                )
            else:
                ctx = self._uia_gate.run_with_timeout(
                    lambda: self._uia.focused_element_context(),
                    timeout=0.75,
                )
            element_context = ElementContext(**ctx) if ctx else None

        semantic_nodes = self._semantic_nodes_from_context(element_context, job)
        tree_quality = "poor" if semantic_nodes else "empty"
        state_text = self._build_state_text(
            app_name=app_name,
            app_bundle_id=app_bundle_id,
            window_title=window_title,
            semantic_nodes=semantic_nodes,
            tree_quality=tree_quality,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return LearningObservation(
            observation_id=self._new_id("obs"),
            correlation_id=job.correlation_id,
            trigger=job.trigger,
            timestamp=time.perf_counter_ns(),
            app_name=app_name,
            app_bundle_id=app_bundle_id,
            window_title=window_title,
            url=url,
            element_context=element_context,
            state_text=state_text,
            semantic_nodes=semantic_nodes,
            tree_quality=tree_quality,
            capture_backend="none",
            timing={"total_ms": elapsed_ms},
        )

    @staticmethod
    def _semantic_nodes_from_context(
        element_context: ElementContext | None,
        job: _ObservationJob,
    ) -> list[dict[str, Any]]:
        if element_context is None:
            return []
        node = element_context.to_dict()
        node["element_index"] = 0
        node["source"] = "uia"
        if job.x is not None and job.y is not None:
            node["center_x"] = job.x
            node["center_y"] = job.y
        return [node]

    @staticmethod
    def _build_state_text(
        *,
        app_name: str | None,
        app_bundle_id: str | None,
        window_title: str | None,
        semantic_nodes: list[dict[str, Any]],
        tree_quality: str,
    ) -> str:
        try:
            from computer_server.services.agent_state_service import build_agent_state

            state = build_agent_state(
                {
                    "window": {
                        "app": app_name,
                        "process_name": app_bundle_id,
                        "title": window_title,
                    },
                    "nodes": semantic_nodes,
                    "node_count": len(semantic_nodes),
                    "filtered_node_count": len(semantic_nodes),
                    "returned_node_count": len(semantic_nodes),
                    "tree_quality": tree_quality,
                    "uia_quality": tree_quality,
                    "msaa_quality": "empty",
                    "vision_quality": "empty",
                    "observation_recommendation": "use_semantic_nodes"
                    if semantic_nodes
                    else "observe_again_if_needed",
                },
                text_limit=500,
            )
            return str(state.get("text") or "")
        except Exception:
            lines = [f"Window: {app_name or 'Unknown'}"]
            if window_title:
                lines.append(f'Title: "{window_title}"')
            if semantic_nodes:
                node = semantic_nodes[0]
                role = node.get("role", "node")
                label = node.get("title") or node.get("computed_name") or ""
                lines.append(f"Element: {role} {label}".strip())
            return "\n".join(lines)

    def _complete_observation(
        self,
        job: _ObservationJob,
        observation: LearningObservation,
    ) -> None:
        with self._lock:
            if self._session is None:
                return
            self._session.observations.append(observation)
            if job.action_id is not None:
                for action in reversed(self._session.actions):
                    if action.action_id == job.action_id:
                        action.after_observation_id = observation.observation_id
                        break
                self._session.links.append(
                    LearningLink(
                        action_id=job.action_id,
                        observation_id=observation.observation_id,
                        correlation_id=job.correlation_id,
                        trigger=job.trigger,
                    )
                )
            self._session.metrics["pending_observations"] = max(
                0,
                self._session.metrics["pending_observations"] - 1,
            )
            self._session.metrics["observation_count"] = len(self._session.observations)
            self._session.metrics["last_observation_at"] = observation.timestamp
            if observation.skip_reason == "timeout":
                self._session.metrics["timeout_count"] += 1
            if observation.skip_reason == "sensitive_context":
                self._session.metrics["sensitive_skip_count"] += 1
            if observation.error:
                self._session.metrics["error_count"] += 1

    # ================================================================
    # Event handlers (called from pynput listener threads)
    #
    # Pattern (matches Swift CGEvent callback):
    #   1. Check session exists under lock, then release
    #   2. Do heavy work outside lock (UIA, coalescer, app detection)
    #   3. _append() acquires lock internally for the brief session write
    # ================================================================

    def _on_key_down(self, vk_code: int, char: Optional[str],
                     modifiers: list[str], is_special: bool) -> None:
        """Called from InputCapture on a listener thread."""
        if self._drop_agent_input_event():
            return

        with self._lock:
            if self._session is None or self._stopping:
                return
            app_detector = self._app_detector
            uia = self._uia

        if app_detector is None:
            return

        name, exe, title = app_detector.current_app_info()

        # Check restricted process
        if exe in RESTRICTED_PROCESSES:
            return

        # Check app switch (may call back to _on_app_switch -> _append)
        app_detector.check_app_switch_with_info(name, exe, self)

        # Check password field (guarded by timeout to avoid hung UIA provider)
        is_secure = self._uia_gate.run_with_timeout(
            lambda: uia.is_password_field_focused(), timeout=0.5,
        ) if uia is not None else False
        if is_secure:
            if self._keystroke_buncher:
                self._keystroke_buncher.flush()
            self._append(ObservedAction(
                timestamp=time.perf_counter_ns(),
                action_type=ObservedActionType.SECURE_FIELD,
                app_name=name,
                app_bundle_id=exe,
                window_title=title,
            ))
            return

        # Determine if this should be coalesced
        if is_special or modifiers:
            if self._keystroke_buncher:
                self._keystroke_buncher.flush()
            key_name = KEY_CODE_TO_NAME.get(vk_code, f"key{vk_code}")
            action_type = ObservedActionType.HOTKEY if modifiers else ObservedActionType.KEY_PRESS
            self._append(ObservedAction(
                timestamp=time.perf_counter_ns(),
                action_type=action_type,
                app_name=name,
                app_bundle_id=exe,
                window_title=title,
                key_code=vk_code,
                key_name=key_name,
                modifiers=modifiers if modifiers else None,
            ))
        else:
            # Character key without modifiers — coalesce into typeText
            if char and self._keystroke_buncher:
                ec = None
                url = None
                if self._keystroke_buncher.is_empty():
                    ctx = self._uia_gate.run_with_timeout(
                        lambda: uia.focused_element_context(), timeout=0.5,
                    ) if uia is not None else None
                    ec = ElementContext(**ctx) if ctx else None
                    url = self._uia_gate.run_with_timeout(
                        lambda: self._get_current_url(exe), timeout=0.5,
                    )
                    # Snapshot UIA value at bunch start for diff extraction
                    last_uia_value = self._uia_gate.run_with_timeout(
                        lambda: uia.read_focused_value(), timeout=0.5,
                    ) if uia is not None else None
                    with self._lock:
                        if not self._stopping:
                            self._last_uia_value = last_uia_value
                self._keystroke_buncher.append(
                    char=char,
                    app_name=name,
                    bundle_id=exe,
                    window_title=title,
                    url=url,
                    element_context=ec,
                )

    def _on_mouse_down(self, x: float, y: float, button: str, count: int) -> None:
        """Called from InputCapture on a listener thread."""
        if self._drop_agent_input_event():
            return

        with self._lock:
            if self._session is None or self._stopping:
                return
            app_detector = self._app_detector
            uia = self._uia

        if app_detector is None:
            return

        # Flush coalesced events before recording the click
        if self._keystroke_buncher:
            self._keystroke_buncher.flush()
        if self._scroll_buncher:
            self._scroll_buncher.flush()

        name, exe, title = app_detector.current_app_info()

        # Track last click position for IME OCR focus region
        self._last_click_x = x
        self._last_click_y = y

        # Check restricted process
        if exe in RESTRICTED_PROCESSES:
            return

        # Check app switch
        app_detector.check_app_switch_with_info(name, exe, self)

        # UIA enrich in real-time (~1ms)
        ec = None
        if uia is not None:
            ctx = self._uia_gate.run_with_timeout(
                lambda: uia.enrich_click(x, y), timeout=0.75,
            )
            if ctx:
                ec = ElementContext(**ctx)

        # URL detection runs on daemon thread with timeout
        url = self._uia_gate.run_with_timeout(
            lambda: self._get_current_url(exe), timeout=0.5,
        )

        action = ObservedAction(
            timestamp=time.perf_counter_ns(),
            action_type=ObservedActionType.CLICK,
            app_name=name,
            app_bundle_id=exe,
            window_title=title,
            url=url,
            element_context=ec,
            x=x, y=y,
            button=button,
            count=count,
        )

        # For clicks without UIA labels, capture a small region around the
        # click point for later OCR enrichment at stop time.
        # Skip when the click context is sensitive (password fields, etc.)
        # to avoid saving sensitive screen content via OCR screenshots.
        if (ec is None or (not ec.title and not ec.identifier)) \
                and not self._is_sensitive_context(uia, ec):
            try:
                from .platform import Screenshotter
                png = Screenshotter().capture_region(
                    max(0, int(x - 50)), max(0, int(y - 15)), 100, 40,
                )
                if png:
                    action.ocr_region = png
            except Exception:
                pass

        self._append(action, observation_trigger="click")

    def _on_scroll(self, x: float, y: float, dx: int, dy: int) -> None:
        """Called from InputCapture on a listener thread."""
        if self._drop_agent_input_event():
            return

        with self._lock:
            if self._session is None or self._stopping:
                return
            app_detector = self._app_detector

        if app_detector is None:
            return

        if self._keystroke_buncher:
            self._keystroke_buncher.flush()

        name, exe, _ = app_detector.current_app_info()
        if exe in RESTRICTED_PROCESSES:
            return

        app_detector.check_app_switch_with_info(name, exe, self)

        if self._scroll_buncher:
            self._scroll_buncher.append(
                dx=dx, dy=dy, x=x, y=y,
                app_name=name, bundle_id=exe,
            )

    # ================================================================
    # Coalescer flush callbacks (called on timer threads or listener)
    # ================================================================

    def _on_keystroke_flush(self, text: str, app_name: str, bundle_id: str,
                             window_title: str, url: str,
                             element_context) -> None:
        # Probe UIA for the real text content (pynput raw keystrokes miss IME chars).
        # Diff against the value at bunch start to extract only newly typed text.
        with self._lock:
            uia = self._uia
            prev = self._last_uia_value or ""
            self._last_uia_value = None
        if uia is not None:
            current = self._uia_gate.run_with_timeout(
                lambda: uia.read_focused_value(), timeout=0.5,
            )
            if current and current.strip():
                if current.startswith(prev) and len(current) > len(prev):
                    text = current[len(prev):]
                elif not current.isascii():
                    # Full replacement: entire value is non-ASCII (e.g. form field)
                    text = current
        action = ObservedAction(
            timestamp=time.perf_counter_ns(),
            action_type=ObservedActionType.TYPE_TEXT,
            app_name=app_name,
            app_bundle_id=bundle_id,
            window_title=window_title,
            url=url,
            element_context=element_context,
            text=text,
        )

        # If UIA couldn't help and text is all ASCII (likely pinyin),
        # capture the active window NOW for later OCR at stop time.
        # Re-check sensitive context before capturing — if the focused
        # field is a password field (or element_context matches sensitive
        # patterns), skip the full-window OCR screenshot.
        if text.isascii() \
                and not self._is_sensitive_context(self._uia, element_context):
            try:
                from .platform import Screenshotter
                png, win_x, win_y = Screenshotter().capture_active_window()
                if png:
                    action.ocr_region = png
                    action.ocr_window_x = win_x
                    action.ocr_window_y = win_y
                    action.ocr_ref_x = int(self._last_click_x)
                    action.ocr_ref_y = int(self._last_click_y)
            except Exception:
                pass

        self._append(action, observation_trigger="typing_pause")

    def _on_scroll_flush(self, dx: int, dy: int, x: float, y: float,
                         app_name: str, bundle_id: str) -> None:
        self._append(ObservedAction(
            timestamp=time.perf_counter_ns(),
            action_type=ObservedActionType.SCROLL,
            app_name=app_name,
            app_bundle_id=bundle_id,
            x=x, y=y,
            delta_x=dx, delta_y=dy,
        ), observation_trigger="scroll_stop")

    # ================================================================
    # App switch handler
    # ================================================================

    def _on_app_switch(self, to_app: str, to_bundle_id: str,
                       from_app: str) -> None:
        """Called by AppDetector when the foreground app changes."""
        self._append(ObservedAction(
            timestamp=time.perf_counter_ns(),
            action_type=ObservedActionType.APP_SWITCH,
            app_name=from_app,
            app_bundle_id=None,
            to_app=to_app,
            to_bundle_id=to_bundle_id,
        ), observation_trigger="app_switch")

    # ================================================================
    # Internal helpers
    # ================================================================

    def _append(
        self,
        action: ObservedAction,
        observation_trigger: str | None = None,
    ) -> bool:
        """Append an action to the session. Acquires lock internally."""
        accepted = False
        with self._lock:
            if self._session is None:
                return False
            if action.action_id is None:
                action.action_id = self._new_id("act")
            if action.correlation_id is None:
                action.correlation_id = self._new_id("corr")
            self._session.actions.append(action)
            self._session.metrics["action_count"] = len(self._session.actions)
            self._session.metrics["last_event_at"] = action.timestamp
            if action.app_name:
                self._session.apps.add(action.app_name)
            if action.url:
                self._session.urls.add(action.url)
            accepted = True
        if accepted and observation_trigger is not None:
            self._queue_observation_for_action(action, observation_trigger)
        return accepted

    def _drop_agent_input_event(self) -> bool:
        with self._lock:
            if self._session is None or self._stopping:
                return False
            return (
                self._agent_input_suppression_count > 0
                or time.monotonic() < self._agent_input_suppressed_until
            )

    def _get_current_url(self, exe: str) -> Optional[str]:
        """Get the current URL if the foreground app is a browser."""
        try:
            if exe.lower() in BROWSER_PROCESSES:
                return self._read_browser_url()
        except Exception:
            pass
        return None

    def _is_sensitive_context(self, uia, element_context=None) -> bool:
        """Check if the current UI context is sensitive (password field, etc.).

        Two-tier check:
        1. UIA: whether the focused element is a password field
           (guarded by timeout — None/timed-out is treated as NOT sensitive
           to avoid blocking legitimate recordings on slow UIA providers).
        2. Element context: whether title/identifier matches
           SENSITIVE_FIELD_PATTERNS keywords.

        Returns True if OCR / screenshots should be skipped for this context.
        """
        if uia is not None:
            is_secure = self._uia_gate.run_with_timeout(
                lambda: uia.is_password_field_focused(), timeout=0.5,
            )
            if is_secure:  # Explicit True only; None (timeout) → not sensitive
                return True

        if element_context is not None:
            for attr in ("title", "identifier"):
                val = getattr(element_context, attr, None)
                if val and isinstance(val, str):
                    val_lower = val.lower()
                    for pattern in SENSITIVE_FIELD_PATTERNS:
                        if pattern in val_lower:
                            return True

        return False

    def _read_browser_url(self) -> Optional[str]:
        """Read the URL from a browser's address bar via UIA.

        Walks the UIA tree looking for an Edit control whose name/description
        contains 'address' or 'url'. Mirrors Swift EventHandlers.currentURL().
        """
        if not self._uia:
            return None
        try:
            from .platform import _ensure_com_initialized
            _ensure_com_initialized()
            import uiautomation as auto

            focused = auto.GetFocusedControl()
            if focused is None:
                return None

            # Find the top-level window (max 64 levels to guard against cycles)
            window = focused
            for _ in range(64):
                try:
                    p = window.GetParentControl()
                    if p is None:
                        break
                    window = p
                except Exception:
                    break

            return self._find_url_in_tree(window, depth=0)
        except Exception:
            return None

    def _find_url_in_tree(self, el, depth: int,
                          visited: set | None = None) -> Optional[str]:
        """Recursive search for address bar element, max depth 8."""
        if depth >= 8:
            return None

        if visited is None:
            visited = set()

        # Guard against UIA tree cycles from buggy providers.
        el_id = id(el)
        if el_id in visited:
            return None
        visited.add(el_id)

        try:
            ct = str(el.ControlType) if el.ControlType else ""
            if "Edit" in ct or "ComboBox" in ct:
                name = (el.Name or "").lower()
                desc = (el.AutomationId or "").lower()
                if any(kw in name or kw in desc
                       for kw in ("address", "url", "search or enter", "omnibox")):
                    try:
                        val = el.GetValuePattern().Value
                        if val:
                            return val
                    except Exception:
                        pass
                    try:
                        val = el.Name
                        if val and ("http" in val or "://" in val):
                            return val
                    except Exception:
                        pass

            children = el.GetChildren()
            for child in children:
                result = self._find_url_in_tree(child, depth + 1, visited)
                if result:
                    return result
        except Exception:
            pass
        return None

    def _finish_stop(self) -> tuple:
        """Execute the stop sequence after _stopping=True has been set.

        Extracted so both user stop() and _on_max_duration share the same
        teardown path without re-acquiring the lock for the initial check.
        """
        if self._max_duration_timer:
            self._max_duration_timer.cancel()
            self._max_duration_timer = None

        if self._input:
            self._input.stop()

        if self._keystroke_buncher:
            self._keystroke_buncher.flush()
        if self._scroll_buncher:
            self._scroll_buncher.flush()

        self._queue_manual_stop_observation()
        observations_flushed = self._wait_for_observation_idle(
            OBSERVATION_STOP_TIMEOUT_SECONDS,
        )
        if not observations_flushed:
            logger.warning("Learning observation flush timed out; returning recorded actions")

        with self._lock:
            if self._session is None:
                self._cleanup()
                return (False, LearningError.NOT_RECORDING)

            session = self._session
            self._session = None

        actions = session.actions

        if not actions:
            self._cleanup()
            return (False, LearningError.NO_ACTIONS_RECORDED)

        # Batch YOLO+OCR vision enrichment for click actions.
        # Runs on a daemon thread with a generous timeout — enrichment
        # is non-fatal: failures never lose the recorded actions.
        def _enrich():
            try:
                self._enrich_vision(actions)
            except Exception:
                logger.debug("Vision enrichment failed", exc_info=True)

        t = threading.Thread(target=_enrich, daemon=True)
        t.start()
        t.join(timeout=30.0)
        if t.is_alive():
            logger.warning("Vision enrichment timed out after 30s — skipping")

        # Save results to disk
        if self._output_dir:
            try:
                self._save_session(session, actions)
            except Exception:
                logger.debug("Failed to save learning session to disk", exc_info=True)

        logger.info("Learning: stopped, recorded %d actions", len(actions))
        self._cleanup()
        return (True, (session, actions))

    def _save_session(self, session, actions: list) -> None:
        """Save session.json and screenshot.png to output_dir."""
        import json
        import os

        os.makedirs(self._output_dir, exist_ok=True)

        data = session.to_dict(
            duration_seconds=int(time.time() - session.start_time),
            actions=actions,
        )
        screenshots_dir = os.path.join(self._output_dir, "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)

        try:
            from .platform import Screenshotter
            png, origin_x, origin_y = Screenshotter().capture_full_with_origin()
            if png:
                with open(os.path.join(screenshots_dir, "full.png"), "wb") as f:
                    f.write(png)
                data["screenshot_origin"] = {"x": origin_x or 0, "y": origin_y or 0}
        except Exception:
            pass

        ocr_idx = 0
        for a in actions:
            png = a.ocr_region
            if png:
                ocr_idx += 1
                fname = f"ocr-{ocr_idx:05d}.png"
                try:
                    with open(os.path.join(screenshots_dir, fname), "wb") as f:
                        f.write(png)
                    a.ocr_image = os.path.join("screenshots", fname)
                except Exception:
                    pass

        data["actions"] = [a.to_dict() for a in actions]
        path = os.path.join(self._output_dir, "session.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    def _enrich_vision(self, actions: list) -> None:
        """Batch-enrich click actions with YOLO+OCR text labels.

        Captures a region around each click point and runs OCR to extract
        the visible text label of the clicked element.  Also OCRs typeText
        regions to recover IME-committed text from pinyin keystrokes.
        Modifies actions in-place.
        """
        try:
            from .platform import VisionEnricher
            enricher = VisionEnricher()
            enricher.enrich_clicks(actions)
            enricher.enrich_type_texts(actions)
        except Exception:
            logger.warning("Vision enrichment failed", exc_info=True)

    def _on_max_duration(self) -> None:
        """Safety timer: auto-stop after MAX_RECORDING_DURATION seconds.

        Holds _lock through the check so a concurrent user stop() sees
        _stopping=True and bails instead of racing for the result.
        """
        with self._lock:
            if self._stopping or self._session is None:
                return
            self._stopping = True

        logger.warning(
            "Learning: max recording duration reached (%ds), auto-stopping",
            MAX_RECORDING_DURATION,
        )
        self._finish_stop()
