"""Thread-safe global singleton element cache supporting UIA + Vision dual sources."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_CACHE_TTL_SECONDS = 600.0


@dataclass
class CachedElement:
    """Represents a single cached UI element from UIA or Vision source."""

    element_index: int  # UIA: 0-based incrementing; Vision: starts from 10000
    source: str  # "uia" or "vision"
    center_x: int
    center_y: int
    bbox: Tuple[int, int, int, int]  # (left, top, right, bottom)
    control_type: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None
    actions: List[str] = field(default_factory=list)
    platform_ref: Any = None  # Deprecated: do not store COM refs across calls
    enabled: bool = True  # From UIA IsEnabledPropertyId; for disabled-element fallback


class ElementCache:
    """Thread-safe global singleton element cache.

    Key = (pid: int, window_id: int)
    Value = List[CachedElement] (ordered by element_index)

    Vision element indices start from 10000 to avoid collision with UIA indices.
    """

    _instance: Optional["ElementCache"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ElementCache":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self._data: Dict[Tuple[int, int], List[CachedElement]] = {}
            self._index: Dict[Tuple[int, int], Dict[int, CachedElement]] = {}
            self._quality: Dict[Tuple[int, int], str] = {}
            self._origins: Dict[Tuple[int, int], Tuple[int, int]] = {}
            self._updated_at: Dict[Tuple[int, int], float] = {}
            self._data_lock = threading.Lock()
            self._initialized = True

    def _is_window_valid(self, window_id: int) -> bool:
        try:
            import win32gui

            return bool(win32gui.IsWindow(int(window_id)))
        except Exception:
            return True

    def _is_entry_current_unlocked(self, key: Tuple[int, int]) -> bool:
        updated_at = self._updated_at.get(key)
        if updated_at is None:
            return False
        if time.monotonic() - updated_at > _CACHE_TTL_SECONDS:
            self._clear_window_unlocked(key)
            return False
        if not self._is_window_valid(key[1]):
            self._clear_window_unlocked(key)
            return False
        return True

    def _clear_window_unlocked(self, key: Tuple[int, int]) -> None:
        self._data.pop(key, None)
        self._index.pop(key, None)
        self._quality.pop(key, None)
        self._origins.pop(key, None)
        self._updated_at.pop(key, None)

    def update(
        self, pid: int, window_id: int, elements: List[CachedElement]
    ) -> None:
        """Replace the entire cache snapshot for the given (pid, window_id)."""
        with self._data_lock:
            key = (pid, window_id)
            self._data[key] = list(elements)
            # Build O(1) lookup index
            self._index[key] = {el.element_index: el for el in elements}
            self._updated_at[key] = time.monotonic()

    def append_vision_elements(
        self, pid: int, window_id: int, elements: List[CachedElement]
    ) -> None:
        """Append vision-detected elements to the existing cache (does not replace UIA elements)."""
        with self._data_lock:
            key = (pid, window_id)
            if key not in self._data:
                self._data[key] = []
            self._data[key].extend(elements)
            # Update index
            if key not in self._index:
                self._index[key] = {}
            for el in elements:
                self._index[key][el.element_index] = el
            self._updated_at[key] = time.monotonic()

    def get_element(
        self, pid: int, window_id: int, index: int
    ) -> Optional[CachedElement]:
        """Get a cached element by its element_index."""
        with self._data_lock:
            key = (pid, window_id)
            if not self._is_entry_current_unlocked(key):
                return None
            idx_map = self._index.get(key)
            if idx_map is None:
                return None
            return idx_map.get(index)

    def get_center(
        self, pid: int, window_id: int, index: int
    ) -> Optional[Tuple[int, int]]:
        """Get the center coordinates (center_x, center_y) of the specified element."""
        el = self.get_element(pid, window_id, index)
        if el is None:
            return None
        return (el.center_x, el.center_y)

    def find_by_text(
        self, pid: int, window_id: int, text: str
    ) -> Optional[CachedElement]:
        """Find the first cached element whose name or value contains text (case-insensitive)."""
        if not text:
            return None
        needle = text.lower()
        key = (pid, window_id)
        with self._data_lock:
            elements = self._data.get(key)
            if not self._is_entry_current_unlocked(key):
                return None
            elements = self._data.get(key)
            if not elements:
                return None
            for el in elements:
                if el.name and needle in el.name.lower():
                    return el
                if el.value and needle in el.value.lower():
                    return el
        return None

    def get_quality(self, pid: int, window_id: int) -> Optional[str]:
        """Get the tree quality assessment for the specified window."""
        key = (pid, window_id)
        with self._data_lock:
            if not self._is_entry_current_unlocked(key):
                return None
            return self._quality.get(key)

    def set_quality(self, pid: int, window_id: int, quality: str) -> None:
        """Set the tree quality assessment for the specified window."""
        key = (pid, window_id)
        with self._data_lock:
            self._quality[key] = quality

    def set_window_origin(self, pid: int, window_id: int, x: int, y: int) -> None:
        """Store the window's screen-space origin for later coordinate translation."""
        key = (pid, window_id)
        with self._data_lock:
            self._origins[key] = (x, y)
            self._updated_at[key] = time.monotonic()

    def get_window_origin(self, pid: int, window_id: int) -> Tuple[int, int]:
        """Get the window's screen-space origin, or (0, 0) if not stored."""
        key = (pid, window_id)
        with self._data_lock:
            if not self._is_entry_current_unlocked(key):
                return (0, 0)
            return self._origins.get(key, (0, 0))

    def get_window_origin_optional(
        self, pid: int, window_id: int
    ) -> Optional[Tuple[int, int]]:
        """Get the window's screen-space origin, or None if not stored.

        Prefer this over ``get_window_origin`` when callers need to
        distinguish "real origin (0, 0)" from "cache has no origin entry".
        """
        key = (pid, window_id)
        with self._data_lock:
            if not self._is_entry_current_unlocked(key):
                return None
            return self._origins.get(key)

    def clear(self) -> None:
        """Clear all caches."""
        with self._data_lock:
            self._data.clear()
            self._index.clear()
            self._quality.clear()
            self._origins.clear()
            self._updated_at.clear()

    def clear_window(self, pid: int, window_id: int) -> None:
        """Clear the cache for the specified window."""
        key = (pid, window_id)
        with self._data_lock:
            self._clear_window_unlocked(key)


def get_element_cache() -> ElementCache:
    """Get the global ElementCache singleton."""
    return ElementCache()
