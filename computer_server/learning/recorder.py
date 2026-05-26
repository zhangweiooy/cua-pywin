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
import threading
import time
from typing import Optional

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
    LearningSession,
    ObservedAction,
    ObservedActionType,
)

logger = logging.getLogger(__name__)


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

    def _cleanup(self):
        self._input = None
        self._uia = None
        self._app_detector = None
        self._keystroke_buncher = None
        self._scroll_buncher = None
        self._last_uia_value = None

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
        is_secure = self._run_uia_with_timeout(
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
                    ctx = self._run_uia_with_timeout(
                        lambda: uia.focused_element_context(), timeout=0.5,
                    ) if uia is not None else None
                    ec = ElementContext(**ctx) if ctx else None
                    url = self._run_uia_with_timeout(
                        lambda: self._get_current_url(exe), timeout=0.5,
                    )
                    # Snapshot UIA value at bunch start for diff extraction
                    last_uia_value = self._run_uia_with_timeout(
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
            ctx = self._run_uia_with_timeout(
                lambda: uia.enrich_click(x, y), timeout=0.75,
            )
            if ctx:
                ec = ElementContext(**ctx)

        # URL detection runs on daemon thread with timeout
        url = self._run_uia_with_timeout(
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
                    action._ocr_region = png  # type: ignore[attr-defined]
            except Exception:
                pass

        self._append(action)

    def _on_scroll(self, x: float, y: float, dx: int, dy: int) -> None:
        """Called from InputCapture on a listener thread."""
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
            current = self._run_uia_with_timeout(
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
                    action._ocr_region = png  # type: ignore[attr-defined]
                    action._ocr_win_x = win_x   # type: ignore[attr-defined]
                    action._ocr_win_y = win_y   # type: ignore[attr-defined]
                    action.ocr_ref_x = int(self._last_click_x)
                    action.ocr_ref_y = int(self._last_click_y)
            except Exception:
                pass

        self._append(action)

    def _on_scroll_flush(self, dx: int, dy: int, x: float, y: float,
                         app_name: str, bundle_id: str) -> None:
        self._append(ObservedAction(
            timestamp=time.perf_counter_ns(),
            action_type=ObservedActionType.SCROLL,
            app_name=app_name,
            app_bundle_id=bundle_id,
            x=x, y=y,
            delta_x=dx, delta_y=dy,
        ))

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
        ))

    # ================================================================
    # Internal helpers
    # ================================================================

    def _append(self, action: ObservedAction) -> None:
        """Append an action to the session. Acquires lock internally."""
        with self._lock:
            if self._session is None:
                return
            self._session.actions.append(action)
            if action.app_name:
                self._session.apps.add(action.app_name)
            if action.url:
                self._session.urls.add(action.url)

    def _get_current_url(self, exe: str) -> Optional[str]:
        """Get the current URL if the foreground app is a browser."""
        try:
            if exe.lower() in BROWSER_PROCESSES:
                return self._read_browser_url()
        except Exception:
            pass
        return None

    def _run_uia_with_timeout(self, func, timeout: float = 0.5):
        """Run a UIA call on a daemon thread, return result or None on timeout.

        Gated by a semaphore so at most one UIA call is in flight at a time.
        If a foreground app's UIA provider is hung (holding the semaphore),
        subsequent calls are skipped immediately — no thread accumulation.
        """
        return self._uia_gate.run_with_timeout(func, timeout=timeout)

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
            is_secure = self._run_uia_with_timeout(
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

        serialized = [a.to_dict() for a in actions]
        data = {
            "task_description": session.task_description or "",
            "duration_seconds": int(time.time() - session.start_time),
            "action_count": len(actions),
            "apps": list(session.apps),
            "urls": list(session.urls),
            "actions": serialized,
        }
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
            png = getattr(a, "_ocr_region", None)
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
