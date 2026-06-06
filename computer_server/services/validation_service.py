"""Shared parameter validation for MCP tool entrypoints."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Optional, Sequence


class ValidationError(ValueError):
    """User-facing validation failure."""


def error_result(exc: Exception) -> dict[str, Any]:
    return {"success": False, "error": str(exc)}


def _display_allowed(values: Iterable[str]) -> str:
    return ", ".join(sorted(values))


def validate_choice(name: str, value: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    normalized = value.lower()
    if normalized not in allowed:
        raise ValidationError(
            f"{name} must be one of: {_display_allowed(allowed)}"
        )
    return normalized


def validate_int_range(
    name: str,
    value: int,
    min_value: int,
    max_value: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if value < min_value or value > max_value:
        raise ValidationError(f"{name} must be between {min_value} and {max_value}")
    return value


def validate_float_range(
    name: str,
    value: float,
    min_value: float,
    max_value: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValidationError(f"{name} must be finite")
    if value < min_value or value > max_value:
        raise ValidationError(f"{name} must be between {min_value} and {max_value}")
    return value


def validate_coordinate(name: str, value: int) -> int:
    return validate_int_range(name, value, -100_000, 100_000)


def validate_optional_coordinate(name: str, value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    return validate_coordinate(name, value)


def validate_mouse_button(button: str) -> str:
    return validate_choice("button", button, {"left", "right", "middle"})


def validate_secondary_action(action: str) -> str:
    return validate_choice("action", action, {"expand"})


def validate_optional_query(name: str, value: Optional[str], max_length: int = 256) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValidationError(f"{name} must not be empty")
    if len(value) > max_length:
        raise ValidationError(f"{name} must be at most {max_length} characters")
    return value


def validate_click_count(count: int) -> int:
    return validate_int_range("count", count, 1, 3)


def validate_drag_duration(duration: float) -> float:
    return validate_float_range("duration", duration, 0.0, 30.0)


def validate_scroll_direction(direction: Optional[str]) -> Optional[str]:
    if direction is None:
        return None
    return validate_choice("direction", direction, {"up", "down", "left", "right"})


def validate_scroll_by(by: str) -> str:
    return validate_choice("by", by, {"line", "page"})


def validate_scroll_amount(amount: int) -> int:
    return validate_int_range("amount", amount, 1, 1000)


def validate_scroll_delta(name: str, value: int) -> int:
    return validate_int_range(name, value, -10_000, 10_000)


def validate_modifiers(modifier: Optional[Sequence[str]]) -> Optional[list[str]]:
    if modifier is None:
        return None
    if len(modifier) > 4:
        raise ValidationError("modifier may contain at most 4 keys")
    allowed = {"ctrl", "control", "alt", "option", "shift", "win", "cmd", "meta"}
    normalized = []
    for key in modifier:
        normalized.append(validate_choice("modifier", key, allowed))
    return normalized


def validate_coordinate_space(coordinate_space: Optional[str]) -> Optional[str]:
    if coordinate_space is None:
        return None
    return validate_choice(
        "coordinate_space",
        coordinate_space,
        {"screen", "window_resized", "window_full_res", "zoom", "element"},
    )


def resolve_coordinate_space(
    coordinate_space: Optional[str],
    *,
    element_index: Optional[int],
    from_zoom: bool = False,
    zoom_id: Optional[str] = None,
    pid: Optional[int] = None,
    window_id: Optional[int] = None,
    supports_zoom: bool = True,
) -> str:
    """Return the effective coordinate space for pointer-style tools.

    ``None`` preserves the default behavior:
    element index -> element, zoom args -> zoom, pid/window_id -> window_resized,
    otherwise screen.
    """
    space = validate_coordinate_space(coordinate_space)
    uses_zoom = bool(from_zoom or zoom_id)

    if space is None:
        if element_index is not None:
            if uses_zoom:
                raise ValidationError("element_index cannot be combined with zoom coordinates")
            return "element"
        if uses_zoom:
            if not supports_zoom:
                raise ValidationError("zoom coordinates are not supported for this tool")
            return "zoom"
        if pid is not None and window_id is not None:
            return "window_resized"
        return "screen"

    if space == "element":
        if element_index is None:
            raise ValidationError("coordinate_space='element' requires element_index")
        if uses_zoom:
            raise ValidationError("coordinate_space='element' cannot be combined with zoom coordinates")
        return space

    if element_index is not None:
        raise ValidationError("element_index requires coordinate_space='element'")

    if space == "zoom":
        if not supports_zoom:
            raise ValidationError("zoom coordinates are not supported for this tool")
        if not zoom_id and (pid is None or window_id is None):
            raise ValidationError("coordinate_space='zoom' requires zoom_id or pid and window_id")
        return space

    if uses_zoom:
        raise ValidationError("from_zoom and zoom_id require coordinate_space='zoom'")

    if space in {"window_resized", "window_full_res"} and (
        pid is None or window_id is None
    ):
        raise ValidationError(
            f"coordinate_space='{space}' requires pid and window_id or snapshot_id"
        )

    return space


def validate_shell(shell: str) -> str:
    return validate_choice("shell", shell, {"powershell", "cmd"})


def validate_timeout(timeout: Optional[float]) -> Optional[float]:
    if timeout is None:
        return None
    return validate_float_range("timeout", timeout, 0.001, 3600.0)


def validate_screenshot_request(format: str, quality: int) -> tuple[str, int]:
    if not isinstance(format, str):
        raise ValidationError("format must be a string")
    fmt = format.lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in {"jpeg", "png"}:
        raise ValidationError("format must be one of: jpeg, jpg, png")
    if fmt == "jpeg":
        quality = validate_int_range("quality", quality, 1, 95)
    return fmt, quality


def validate_max_image_dimension(max_image_dimension: int) -> int:
    return validate_int_range("max_image_dimension", max_image_dimension, 64, 4096)


def validate_zoom_rect(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
    x1 = validate_coordinate("x1", x1)
    y1 = validate_coordinate("y1", y1)
    x2 = validate_coordinate("x2", x2)
    y2 = validate_coordinate("y2", y2)
    if x2 <= x1:
        raise ValidationError("x2 must be greater than x1")
    if y2 <= y1:
        raise ValidationError("y2 must be greater than y1")
    return x1, y1, x2, y2


def validate_port(port: Optional[int]) -> Optional[int]:
    if port is None:
        return None
    return validate_int_range("port", port, 1, 65535)


def validate_tab_index(tab_index: int) -> int:
    return validate_int_range("tab_index", tab_index, 0, 1000)


def validate_page_action(action: str) -> str:
    return validate_choice(
        "action",
        action,
        {"get_page_info", "get_text", "query_dom", "execute_javascript"},
    )


_ATTRIBUTE_RE = re.compile(r"^[A-Za-z_:-][A-Za-z0-9_.:-]{0,63}$")


def validate_dom_attributes(
    attributes: Optional[Sequence[str]],
    allow_html: bool,
) -> Optional[list[str]]:
    if attributes is None:
        return None
    if len(attributes) > 32:
        raise ValidationError("attributes may contain at most 32 entries")

    validated: list[str] = []
    for attr in attributes:
        if not isinstance(attr, str):
            raise ValidationError("attributes entries must be strings")
        if attr == "innerHTML" and not allow_html:
            raise ValidationError("innerHTML requires allow_html=True")
        if attr not in {"textContent", "innerHTML"} and not _ATTRIBUTE_RE.match(attr):
            raise ValidationError(
                f"Invalid attribute name {attr!r}; use DOM attribute names or textContent"
            )
        validated.append(attr)
    return validated


def validate_javascript(javascript: Optional[str], allow_javascript: bool) -> str:
    if not javascript:
        raise ValidationError("javascript is required for execute_javascript")
    if not allow_javascript:
        raise ValidationError(
            "execute_javascript requires allow_javascript=True"
        )
    if len(javascript) > 100_000:
        raise ValidationError("javascript must be 100000 characters or fewer")
    return javascript
