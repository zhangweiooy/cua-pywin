"""Window-state screenshot geometry helpers."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
DEFAULT_RETURNED_NODE_LIMIT = 100
PUBLIC_FLOAT_DECIMALS = 2

_COMPACT_WINDOW_STATE_OMIT_FIELDS = frozenset({
    "accessibility_hint",
    "agent_state",
    "capture_metadata",
    "merged_node_count",
    "msaa_node_count",
    "msaa_total_elements",
    "msaa_tree_node_count",
    "node_sources",
    "nodes_filtered",
    "provider_errors",
    "total_elements",
    "uia_node_count",
    "unfiltered_node_count",
    "vision_metadata",
    "vision_node_count",
})


@dataclass(frozen=True)
class WindowSnapshotGeometry:
    full_width: int
    full_height: int
    screenshot_origin_x: int
    screenshot_origin_y: int
    window_origin_x: int
    window_origin_y: int
    crop_origin_screen_x: int
    crop_origin_screen_y: int
    crop_origin_window_x: int
    crop_origin_window_y: int
    original_width: int
    original_height: int
    screen_width: int
    screen_height: int
    compression_ratio: float

    def to_zoom_metadata(self) -> Dict[str, Any]:
        """Return metadata used by zoom/from_zoom coordinate translation."""
        return {
            "compression_ratio": self.compression_ratio,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "window_origin_x": self.window_origin_x,
            "window_origin_y": self.window_origin_y,
            "crop_origin_screen_x": self.crop_origin_screen_x,
            "crop_origin_screen_y": self.crop_origin_screen_y,
            "crop_origin_window_x": self.crop_origin_window_x,
            "crop_origin_window_y": self.crop_origin_window_y,
            "screenshot_origin_x": self.screenshot_origin_x,
            "screenshot_origin_y": self.screenshot_origin_y,
        }


def prepare_window_snapshot_image(
    img,
    bounds: Dict[str, Any] | None,
    screenshot_origin: Tuple[int, int],
    max_image_dimension: int,
):
    """Crop a full screenshot to a window and resize it for transport.

    Returns ``(processed_image, geometry)``. Coordinates in geometry preserve
    the distinction between real window origin and visible crop origin, which
    matters for negative monitor coordinates and partially off-screen windows.
    """
    from PIL import Image as PILImage

    full_w, full_h = img.size
    screenshot_origin_x, screenshot_origin_y = screenshot_origin

    win_origin_x = 0
    win_origin_y = 0
    crop_origin_screen_x = 0
    crop_origin_screen_y = 0

    if bounds is not None and bounds["width"] > 0 and bounds["height"] > 0:
        wx = bounds["x"]
        wy = bounds["y"]
        ww = bounds["width"]
        wh = bounds["height"]

        crop_x1 = max(0, wx - screenshot_origin_x)
        crop_y1 = max(0, wy - screenshot_origin_y)
        crop_x2 = min(full_w, wx + ww - screenshot_origin_x)
        crop_y2 = min(full_h, wy + wh - screenshot_origin_y)

        if crop_x2 > crop_x1 and crop_y2 > crop_y1:
            img = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))
            win_origin_x = wx
            win_origin_y = wy
            crop_origin_screen_x = crop_x1 + screenshot_origin_x
            crop_origin_screen_y = crop_y1 + screenshot_origin_y

    crop_origin_window_x = crop_origin_screen_x - win_origin_x
    crop_origin_window_y = crop_origin_screen_y - win_origin_y
    original_width, original_height = img.size

    if original_width > max_image_dimension or original_height > max_image_dimension:
        if original_width > original_height:
            new_w = max_image_dimension
            new_h = int(original_height * max_image_dimension / original_width)
        else:
            new_h = max_image_dimension
            new_w = int(original_width * max_image_dimension / original_height)
        compression_ratio = original_width / new_w
        img = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
        screen_width, screen_height = new_w, new_h
    else:
        compression_ratio = 1.0
        screen_width, screen_height = original_width, original_height

    geometry = WindowSnapshotGeometry(
        full_width=full_w,
        full_height=full_h,
        screenshot_origin_x=screenshot_origin_x,
        screenshot_origin_y=screenshot_origin_y,
        window_origin_x=win_origin_x,
        window_origin_y=win_origin_y,
        crop_origin_screen_x=crop_origin_screen_x,
        crop_origin_screen_y=crop_origin_screen_y,
        crop_origin_window_x=crop_origin_window_x,
        crop_origin_window_y=crop_origin_window_y,
        original_width=original_width,
        original_height=original_height,
        screen_width=screen_width,
        screen_height=screen_height,
        compression_ratio=compression_ratio,
    )
    return img, geometry


def prepare_direct_window_snapshot_image(
    img,
    bounds: Dict[str, Any] | None,
    max_image_dimension: int,
):
    """Resize a direct target-window capture and build window-local geometry."""
    from PIL import Image as PILImage

    full_w, full_h = img.size
    win_origin_x = int((bounds or {}).get("x", 0) or 0)
    win_origin_y = int((bounds or {}).get("y", 0) or 0)
    original_width, original_height = img.size

    if original_width > max_image_dimension or original_height > max_image_dimension:
        if original_width > original_height:
            new_w = max_image_dimension
            new_h = int(original_height * max_image_dimension / original_width)
        else:
            new_h = max_image_dimension
            new_w = int(original_width * max_image_dimension / original_height)
        compression_ratio = original_width / new_w
        img = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
        screen_width, screen_height = new_w, new_h
    else:
        compression_ratio = 1.0
        screen_width, screen_height = original_width, original_height

    geometry = WindowSnapshotGeometry(
        full_width=full_w,
        full_height=full_h,
        screenshot_origin_x=win_origin_x,
        screenshot_origin_y=win_origin_y,
        window_origin_x=win_origin_x,
        window_origin_y=win_origin_y,
        crop_origin_screen_x=win_origin_x,
        crop_origin_screen_y=win_origin_y,
        crop_origin_window_x=0,
        crop_origin_window_y=0,
        original_width=original_width,
        original_height=original_height,
        screen_width=screen_width,
        screen_height=screen_height,
        compression_ratio=compression_ratio,
    )
    return img, geometry


def project_nodes_to_window_resized(
    nodes: List[Dict[str, Any]],
    geometry: WindowSnapshotGeometry,
) -> List[Dict[str, Any]]:
    """Project internal window-full-res node geometry onto the returned screenshot."""
    ratio = geometry.compression_ratio or 1.0
    offset_x = geometry.crop_origin_window_x
    offset_y = geometry.crop_origin_window_y

    def _project_x(value: Any) -> int:
        return max(
            0,
            min(
                geometry.screen_width,
                int(round((int(value) - offset_x) / ratio)),
            ),
        )

    def _project_y(value: Any) -> int:
        return max(
            0,
            min(
                geometry.screen_height,
                int(round((int(value) - offset_y) / ratio)),
            ),
        )

    projected: List[Dict[str, Any]] = []
    for node in nodes:
        public_node = dict(node)
        bbox = node.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            public_node["bbox"] = [
                _project_x(bbox[0]),
                _project_y(bbox[1]),
                _project_x(bbox[2]),
                _project_y(bbox[3]),
            ]
        if node.get("center_x") is not None:
            public_node["center_x"] = _project_x(node["center_x"])
        if node.get("center_y") is not None:
            public_node["center_y"] = _project_y(node["center_y"])
        projected.append(public_node)
    return projected


def classify_tree_quality(count: int) -> str:
    """Classify element count as tree quality: empty/poor/ok/rich."""
    if count == 0:
        return "empty"
    if count > 50:
        return "rich"
    if count > 15:
        return "ok"
    return "poor"


class WindowOperationGate:
    """Per-window gate that prevents stacked calls on hung accessibility providers."""

    def __init__(
        self,
        unhealthy_cooldown_s: float = 30.0,
        operation_name: str = "accessibility operation",
        fallback_hint: str = "falling back",
    ) -> None:
        self._lock = threading.Lock()
        self._inflight: set[Tuple[int, int]] = set()
        self._unhealthy_until: Dict[Tuple[int, int], float] = {}
        self._unhealthy_cooldown_s = unhealthy_cooldown_s
        self._operation_name = operation_name
        self._fallback_hint = fallback_hint

    @property
    def unhealthy_cooldown_s(self) -> float:
        return self._unhealthy_cooldown_s

    @property
    def operation_name(self) -> str:
        return self._operation_name

    def claim(self, pid: int, window_id: int) -> tuple[Tuple[int, int], bool, Optional[str]]:
        """Try to claim one operation slot for a window."""
        gate_key = (int(pid), int(window_id))
        now = time.monotonic()
        with self._lock:
            unhealthy_until = self._unhealthy_until.get(gate_key, 0.0)
            if unhealthy_until > now:
                return (
                    gate_key,
                    False,
                    "%s skipped; window is in unhealthy cooldown "
                    "(%.1fs remaining); %s."
                    % (self._operation_name, unhealthy_until - now, self._fallback_hint),
                )
            if gate_key in self._inflight:
                return (
                    gate_key,
                    False,
                    "%s skipped; another operation is already in flight "
                    "for this window; %s." % (self._operation_name, self._fallback_hint),
                )

            self._inflight.add(gate_key)
            if unhealthy_until and unhealthy_until <= now:
                self._unhealthy_until.pop(gate_key, None)
            return gate_key, True, None

    def release(self, gate_key: Tuple[int, int]) -> None:
        with self._lock:
            self._inflight.discard(gate_key)

    def mark_unhealthy(self, gate_key: Tuple[int, int]) -> None:
        with self._lock:
            self._unhealthy_until[gate_key] = time.monotonic() + self._unhealthy_cooldown_s


class UIAWalkGate(WindowOperationGate):
    """Per-window gate that prevents stacked UIA walks on hung providers."""

    def __init__(self, unhealthy_cooldown_s: float = 30.0) -> None:
        super().__init__(
            unhealthy_cooldown_s=unhealthy_cooldown_s,
            operation_name="UIA walk",
            fallback_hint="falling back to vision",
        )


class MSAAWalkGate(WindowOperationGate):
    """Per-window gate that prevents stacked MSAA walks on hung providers."""

    def __init__(self, unhealthy_cooldown_s: float = 30.0) -> None:
        super().__init__(
            unhealthy_cooldown_s=unhealthy_cooldown_s,
            operation_name="MSAA walk",
            fallback_hint="skipping MSAA fallback",
        )


class UIAActionGate(WindowOperationGate):
    """Per-window gate that prevents stacked UIA actions on hung providers."""

    def __init__(self, unhealthy_cooldown_s: float = 10.0) -> None:
        super().__init__(
            unhealthy_cooldown_s=unhealthy_cooldown_s,
            operation_name="UIA action",
            fallback_hint="falling back to pixel input",
        )


async def run_window_operation_with_gate(
    *,
    pid: int,
    window_id: int,
    gate: WindowOperationGate,
    func: Callable[[], T],
    timeout_s: float,
) -> tuple[Optional[T], Optional[str]]:
    """Run a blocking window operation with timeout and per-window gating.

    The underlying thread cannot be force-killed if COM hangs. The gate prevents
    repeated calls from stacking on the same provider and marks timeouts
    unhealthy so callers can take a fallback path.
    """
    gate_key, claimed_gate, skip_reason = gate.claim(pid, window_id)
    if skip_reason is not None:
        logger.info(skip_reason)
        return None, skip_reason

    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, func)
        if claimed_gate:
            try:
                future.add_done_callback(lambda _f, _k=gate_key: gate.release(_k))
                claimed_gate = False
            except Exception as exc:
                claimed_gate = False
                gate.mark_unhealthy(gate_key)
                raise exc
        result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        return result, None
    except asyncio.TimeoutError:
        gate.mark_unhealthy(gate_key)
        message = (
            f"{gate.operation_name} timed out after {timeout_s:.1f}s "
            f"(pid={pid}, window_id={window_id})"
        )
        logger.warning(
            "%s; keeping this window gated until the leaked operation thread returns; "
            "marking unhealthy for %.0fs as a secondary guard",
            message,
            gate.unhealthy_cooldown_s,
        )
        return None, message
    except Exception as exc:
        logger.warning("%s failed: %s", gate.operation_name, exc)
        return None, str(exc)
    finally:
        if claimed_gate:
            gate.release(gate_key)


def _quality_rank(quality: str) -> int:
    return {"empty": 0, "poor": 1, "ok": 2, "rich": 3}.get(quality, 0)


def _best_tree_quality(*qualities: str) -> str:
    best = "empty"
    for quality in qualities:
        if _quality_rank(quality) > _quality_rank(best):
            best = quality
    return best


def _merge_provider_errors(*sources: Dict[str, Any]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for source in sources:
        errors = source.get("provider_errors") or {}
        if isinstance(errors, dict):
            for key, value in errors.items():
                if value:
                    merged[str(key)] = str(value)
    return merged


def _semantic_degraded_reason(
    *,
    tree_quality: str,
    uia_data: Dict[str, Any],
    msaa_quality: str,
    vision_quality: str,
    returned_node_count: int,
) -> Optional[str]:
    for source in (uia_data,):
        reason = source.get("degraded_reason")
        if reason:
            return str(reason)
    if tree_quality == "empty" and returned_node_count == 0:
        if uia_data.get("uia_quality") == "empty" and msaa_quality == "empty" and vision_quality == "empty":
            return "semantic_tree_empty"
    if tree_quality in {"empty", "poor"}:
        return f"semantic_tree_{tree_quality}"
    return None


def _sample_window_occlusion(
    window_id: int,
    bounds: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Best-effort screen-crop occlusion sampling for diagnostic metadata."""
    if not bounds or bounds.get("width", 0) <= 0 or bounds.get("height", 0) <= 0:
        return {"status": "unavailable", "reason": "bounds_unavailable"}
    try:
        import win32gui

        left = int(bounds["x"])
        top = int(bounds["y"])
        width = int(bounds["width"])
        height = int(bounds["height"])
        right = left + width - 1
        bottom = top + height - 1
        samples = [
            (left + width // 2, top + height // 2),
            (left + 1, top + 1),
            (max(left + 1, right - 1), top + 1),
            (left + 1, max(top + 1, bottom - 1)),
            (max(left + 1, right - 1), max(top + 1, bottom - 1)),
        ]
        matched = 0
        mismatched = 0
        for point in samples:
            hwnd = win32gui.WindowFromPoint(point)
            try:
                root = win32gui.GetAncestor(hwnd, 2) if hwnd else hwnd
            except Exception:
                root = hwnd
            if int(root or 0) == int(window_id):
                matched += 1
            else:
                mismatched += 1
        if mismatched == 0 and matched > 0:
            return {"status": "clear", "sampled_points": len(samples)}
        return {
            "status": "possibly_occluded",
            "sampled_points": len(samples),
            "matched_points": matched,
            "mismatched_points": mismatched,
        }
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc)}


def _finalize_capture_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    warnings = list(dict.fromkeys(str(w) for w in metadata.get("warnings", []) if w))
    fallback = metadata.get("fallback") if isinstance(metadata.get("fallback"), dict) else None
    provider_errors: Dict[str, str] = {}
    if fallback and fallback.get("reason"):
        provider_errors[str(fallback.get("from_backend") or "capture")] = str(fallback["reason"])
    metadata["warnings"] = warnings
    metadata["provider_errors"] = provider_errors

    degraded_reason: Optional[str] = None
    for warning, reason in (
        ("target_window_minimized", "target_window_minimized"),
        ("target_window_bounds_unavailable", "target_window_bounds_unavailable"),
        ("target_window_possibly_occluded", "screen_crop_occlusion_risk"),
        ("preferred_capture_backend_failed", "preferred_capture_backend_failed"),
    ):
        if warning in warnings:
            degraded_reason = reason
            break
    if degraded_reason is None and metadata.get("occlusion_risk"):
        degraded_reason = "screen_crop_occlusion_risk"
    metadata["degraded_reason"] = degraded_reason

    if metadata.get("capture_backend") == "wgc" and not degraded_reason:
        reliability = "high"
    elif degraded_reason in {None, "preferred_capture_backend_failed"}:
        reliability = "medium"
    else:
        reliability = "low"
    metadata["capture_reliability"] = reliability
    return metadata


def should_collect_msaa(
    uia_data: Dict[str, Any],
    window_state: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether MSAA fallback should run for a window-state request.

    MSAA is cheap enough to collect opportunistically beside UIA, and it can
    preserve classic Win32 roles that UIA proxies collapse. The returned tree quality
    is still only promoted when MSAA contributes usable indexed nodes.
    """
    return os.getenv("CUA_DISABLE_MSAA_FALLBACK") != "1"


def next_element_index(nodes: List[Dict[str, Any]]) -> int:
    """Return the next available element index for merged node sources."""
    indices = [
        node.get("element_index")
        for node in nodes
        if isinstance(node.get("element_index"), int)
    ]
    if not indices:
        return 0
    return max(indices) + 1


async def collect_uia_nodes(
    pid: int,
    window_id: int,
    query: Optional[str],
    win_origin_x: int,
    win_origin_y: int,
    gate: UIAWalkGate,
) -> Dict[str, Any]:
    """Walk the UIA tree and return window-relative node metadata."""
    gate_key, claimed_gate, skip_reason = gate.claim(pid, window_id)
    if skip_reason is not None:
        logger.info(skip_reason)
        return {
            "nodes": [],
            "total_elements": 0,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "empty",
            "tree_quality": "empty",
            "provider_errors": {"uia": skip_reason},
            "degraded_reason": "uia_gate_skipped",
        }

    try:
        from computer_server.handlers.windows_uia import walk_uia_tree

        loop = asyncio.get_running_loop()
        uia_future = loop.run_in_executor(None, walk_uia_tree, int(window_id), query)
        if claimed_gate:
            try:
                uia_future.add_done_callback(lambda _f, _k=gate_key: gate.release(_k))
                claimed_gate = False
            except Exception:
                claimed_gate = False
                gate.mark_unhealthy(gate_key)
                raise

        uia_result = await asyncio.wait_for(asyncio.shield(uia_future), timeout=30.0)
        nodes: List[Dict[str, Any]] = []
        for n in uia_result.indexed_nodes:
            cx = max(0, n.center_x - win_origin_x)
            cy = max(0, n.center_y - win_origin_y)
            bbox = n.bbox
            w_bbox = (
                max(0, bbox[0] - win_origin_x),
                max(0, bbox[1] - win_origin_y),
                max(0, bbox[2] - win_origin_x),
                max(0, bbox[3] - win_origin_y),
            )
            nodes.append({
                "element_index": n.element_index,
                "source": "uia",
                "control_type": n.control_type,
                "name": n.name,
                "value": n.value,
                "center_x": cx,
                "center_y": cy,
                "bbox": list(w_bbox),
                "actions": n.actions,
                "enabled": n.is_enabled,
                "is_offscreen": n.is_offscreen,
                "depth": n.depth,
            })

        return {
            "nodes": nodes,
            "total_elements": uia_result.total_elements,
            "uia_node_count": len(uia_result.indexed_nodes),
            "tree_markdown": uia_result.tree_markdown,
            "uia_quality": uia_result.tree_quality,
            "tree_quality": uia_result.tree_quality,
            "provider_errors": {},
            "degraded_reason": None,
        }
    except asyncio.TimeoutError:
        gate.mark_unhealthy(gate_key)
        logger.warning(
            "Windows UIA tree walk timed out after 30s; keeping "
            "(pid=%s, window_id=%s) gated until the leaked walk thread returns; "
            "marking unhealthy for %.0fs as a secondary guard",
            pid,
            window_id,
            gate.unhealthy_cooldown_s,
        )
        return {
            "nodes": [],
            "total_elements": 0,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "empty",
            "tree_quality": "empty",
            "provider_errors": {"uia": "timeout"},
            "degraded_reason": "uia_timeout",
        }
    except Exception as e:
        logger.warning("Windows UIA tree walk failed: %s", e)
        return {
            "nodes": [],
            "total_elements": 0,
            "uia_node_count": 0,
            "tree_markdown": "",
            "uia_quality": "empty",
            "tree_quality": "empty",
            "provider_errors": {"uia": str(e)},
            "degraded_reason": "uia_error",
        }
    finally:
        if claimed_gate:
            gate.release(gate_key)


async def get_accessibility_tree_snapshot(
    window_id: int = 0,
    *,
    gate: Optional[UIAWalkGate] = None,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Return a protected UIA tree for the standalone accessibility tool."""
    import sys

    if sys.platform != "win32":
        return {"success": False, "error": "UIA is only available on Windows"}

    try:
        import win32gui
        import win32process
        from computer_server.handlers.windows_uia import walk_uia_tree

        hwnd = int(window_id) if window_id else int(win32gui.GetForegroundWindow())
        if not hwnd:
            return {"success": False, "error": "No foreground window found"}
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0

        gate = gate if gate is not None else UIAWalkGate()
        result, gate_error = await run_window_operation_with_gate(
            pid=int(pid),
            window_id=hwnd,
            gate=gate,
            timeout_s=timeout_s,
            func=lambda: walk_uia_tree(hwnd),
        )
        if result is None:
            return {
                "success": False,
                "error": gate_error or "UIA tree walk failed",
                "tree_markdown": "",
                "tree_quality": "empty",
                "node_count": 0,
                "window_id": hwnd,
                "pid": int(pid),
            }

        return {
            "success": True,
            "tree_markdown": result.tree_markdown,
            "tree_quality": result.tree_quality,
            "node_count": result.total_elements,
            "window_id": hwnd,
            "pid": int(pid),
        }
    except Exception as exc:
        logger.error("Error getting accessibility tree: %s", exc)
        return {"success": False, "error": str(exc)}


async def collect_msaa_nodes(
    window_id: int,
    query: Optional[str],
    win_origin_x: int,
    win_origin_y: int,
    index_start: int = 0,
    gate: Optional[MSAAWalkGate] = None,
    pid: Optional[int] = None,
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    """Walk the MSAA tree and return window-relative node metadata."""
    try:
        from computer_server.handlers.windows_msaa import walk_msaa_tree

        gate = gate if gate is not None else MSAAWalkGate()
        gate_pid = int(pid if pid is not None else 0)
        msaa_result, gate_error = await run_window_operation_with_gate(
            pid=gate_pid,
            window_id=int(window_id),
            gate=gate,
            timeout_s=timeout_s,
            func=lambda: walk_msaa_tree(
                int(window_id),
                query,
                index_start=int(index_start),
            ),
        )
        if msaa_result is None:
            if gate_error:
                logger.info("Windows MSAA tree walk skipped: %s", gate_error)
            return {
                "nodes": [],
                "total_elements": 0,
                "msaa_total_elements": 0,
                "msaa_tree_node_count": 0,
                "msaa_node_count": 0,
                "tree_markdown": "",
                "msaa_quality": "empty",
                "provider_errors": {"msaa": gate_error} if gate_error else {},
                "degraded_reason": "msaa_gate_skipped" if gate_error else None,
            }

        nodes: List[Dict[str, Any]] = []
        for n in msaa_result.indexed_nodes:
            cx = max(0, n.center_x - win_origin_x)
            cy = max(0, n.center_y - win_origin_y)
            bbox = n.bbox
            w_bbox = (
                max(0, bbox[0] - win_origin_x),
                max(0, bbox[1] - win_origin_y),
                max(0, bbox[2] - win_origin_x),
                max(0, bbox[3] - win_origin_y),
            )
            nodes.append({
                "element_index": n.element_index,
                "source": "msaa",
                "control_type": n.role,
                "role": n.role,
                "name": n.name,
                "value": n.value,
                "description": n.description,
                "center_x": cx,
                "center_y": cy,
                "bbox": list(w_bbox),
                "actions": n.actions,
                "enabled": True,
                "depth": n.depth,
                "metadata": {
                    "msaa_role": n.role,
                    "msaa_role_id": n.role_id,
                    "msaa_role_hex": f"0x{n.role_id:X}" if n.role_id is not None else None,
                    "msaa_default_action": n.default_action,
                    "msaa_state": n.state,
                    "msaa_child_id": n.child_id,
                    "msaa_has_location": n.bbox != (0, 0, 0, 0),
                },
            })

        return {
            "nodes": nodes,
            "total_elements": msaa_result.total_elements,
            "msaa_total_elements": msaa_result.total_elements,
            "msaa_tree_node_count": len(msaa_result.nodes),
            "msaa_node_count": len(msaa_result.indexed_nodes),
            "tree_markdown": msaa_result.tree_markdown,
            "msaa_quality": msaa_result.tree_quality,
            "provider_errors": {},
            "degraded_reason": None,
        }
    except Exception as e:
        logger.warning("Windows MSAA tree walk failed: %s", e)
        return {
            "nodes": [],
            "total_elements": 0,
            "msaa_total_elements": 0,
            "msaa_tree_node_count": 0,
            "msaa_node_count": 0,
            "tree_markdown": "",
            "msaa_quality": "empty",
            "provider_errors": {"msaa": str(e)},
            "degraded_reason": "msaa_error",
        }


def build_cached_elements(nodes: List[Dict[str, Any]]) -> list:
    """Build ElementCache entries from window-state node dictionaries."""
    from computer_server.handlers.element_cache import CachedElement

    cached_elements = []
    for i, node in enumerate(nodes):
        bbox = node.get("bbox") or (0, 0, 0, 0)
        cached_elements.append(CachedElement(
            element_index=node.get("element_index", i),
            source=node.get("source", "uia"),
            center_x=node.get("center_x", (bbox[0] + bbox[2]) // 2),
            center_y=node.get("center_y", (bbox[1] + bbox[3]) // 2),
            bbox=bbox if len(bbox) == 4 else (0, 0, 0, 0),
            control_type=node.get("control_type") or node.get("role"),
            name=node.get("name") or node.get("title"),
            value=node.get("value"),
            actions=node.get("actions", []),
            platform_ref=None,
            enabled=node.get("enabled", True),
            metadata=node.get("metadata", {}),
        ))
    return cached_elements


def node_matches_query(node: Dict[str, Any], query: Optional[str]) -> bool:
    """Return whether a window-state node matches a user query."""
    if not query:
        return True

    needle = query.lower()
    searchable = [
        node.get("source"),
        node.get("control_type"),
        node.get("role"),
        node.get("name"),
        node.get("title"),
        node.get("value"),
        node.get("icon_label"),
    ]
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        searchable.extend(metadata.values())
    actions = node.get("actions")
    if isinstance(actions, list):
        searchable.extend(str(action) for action in actions)
    elif actions is not None:
        searchable.append(actions)
    action_hints = node.get("action_hints")
    if isinstance(action_hints, list):
        searchable.extend(str(hint) for hint in action_hints)
    elif action_hints is not None:
        searchable.append(action_hints)

    return any(needle in str(value).lower() for value in searchable if value is not None)


def filter_nodes_by_query(
    nodes: List[Dict[str, Any]],
    query: Optional[str],
    include_all_nodes: bool,
) -> List[Dict[str, Any]]:
    """Filter returned nodes while preserving full internal cache data."""
    if include_all_nodes or not query:
        return list(nodes)
    return [node for node in nodes if node_matches_query(node, query)]


INTERACTIVE_CONTROL_HINTS = {
    "button",
    "checkbox",
    "combo",
    "combobox",
    "edit",
    "hyperlink",
    "link",
    "listitem",
    "menu",
    "menuitem",
    "radio",
    "radiobutton",
    "splitbutton",
    "tabitem",
    "textinput",
    "treeitem",
}

TEXT_ENTRY_CONTROL_TYPES = {
    "combobox",
    "document",
    "edit",
    "textinput",
}

PRIMARY_ACTION_CONTROL_TYPES = {
    "button",
    "hyperlink",
    "link",
    "menuitem",
    "splitbutton",
}

TOGGLE_CONTROL_TYPES = {
    "checkbox",
    "radiobutton",
}

SELECTION_CONTROL_TYPES = {
    "dataitem",
    "listitem",
    "tabitem",
    "treeitem",
}

TEXT_CONTENT_CONTROL_TYPES = {
    "label",
    "statictext",
    "text",
}

SCROLL_CONTROL_TYPES = {
    "scrollbar",
}

CONTAINER_CONTROL_HINTS = {
    "custom",
    "group",
    "pane",
    "scrollbar",
    "separator",
    "statusbar",
    "titlebar",
    "toolbar",
    "window",
}


def _has_textual_signal(node: Dict[str, Any]) -> bool:
    for key in (
        "name",
        "title",
        "value",
        "icon_label",
        "raw_caption",
        "normalized_label",
    ):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return True
    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        for key in ("icon_label", "raw_caption", "normalized_label", "ocr_confidence"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                return True
    return False


def _has_action_signal(node: Dict[str, Any]) -> bool:
    actions = node.get("actions")
    if isinstance(actions, list):
        return any(str(action).strip() for action in actions)
    return actions is not None and str(actions).strip() != ""


def _node_actions(node: Dict[str, Any]) -> set[str]:
    actions = node.get("actions")
    if isinstance(actions, list):
        return {str(action).strip().lower() for action in actions if str(action).strip()}
    if actions is None:
        return set()
    action = str(actions).strip().lower()
    return {action} if action else set()


def _normalized_control_type(node: Dict[str, Any]) -> str:
    value = str(node.get("control_type") or node.get("role") or "").lower()
    value = value.removeprefix("controltype.")
    return value.replace(" ", "").replace("_", "").replace("-", "")


def derive_action_hints(node: Dict[str, Any]) -> List[str]:
    """Return agent-facing action hints derived from role and low-level actions."""
    control = _normalized_control_type(node)
    actions = _node_actions(node)
    hints: List[str] = []

    def add(*values: str) -> None:
        for value in values:
            if value and value not in hints:
                hints.append(value)

    if (
        "invoke" in actions
        or "click" in actions
        or control in PRIMARY_ACTION_CONTROL_TYPES
    ):
        add("click", "press")
    if "select" in actions or control in SELECTION_CONTROL_TYPES:
        add("click", "select")
    if "toggle" in actions or control in TOGGLE_CONTROL_TYPES:
        add("click", "toggle")
    if "expand" in actions or control in {"combobox", "splitbutton"}:
        add("expand")
    if "set_value" in actions or control in TEXT_ENTRY_CONTROL_TYPES:
        add("focus", "type")
        if "set_value" in actions:
            add("set_value")
    elif "text" in actions:
        add("focus", "type")
    if "scroll" in actions or control in SCROLL_CONTROL_TYPES:
        add("scroll")

    if node.get("enabled") is False:
        return []
    return hints


def enrich_nodes_with_action_hints(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach action_hints without mutating caller-owned node dictionaries."""
    enriched: List[Dict[str, Any]] = []
    for node in nodes:
        public_node = dict(node)
        public_node["action_hints"] = derive_action_hints(public_node)
        enriched.append(public_node)
    return enriched


def _control_role_score(control: str, has_text: bool) -> float:
    """Rank roles similarly to Tactile's concise element summaries."""
    if control in TEXT_ENTRY_CONTROL_TYPES:
        return 45.0
    if control in PRIMARY_ACTION_CONTROL_TYPES:
        return 40.0
    if control in SELECTION_CONTROL_TYPES:
        return 34.0
    if has_text and control in TEXT_CONTENT_CONTROL_TYPES:
        return 24.0
    if any(hint in control for hint in INTERACTIVE_CONTROL_HINTS):
        return 30.0
    if any(hint in control for hint in CONTAINER_CONTROL_HINTS):
        return -20.0
    return 0.0


def _node_bbox_area(node: Dict[str, Any]) -> Optional[float]:
    bbox = node.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        width = max(0.0, float(bbox[2]) - float(bbox[0]))
        height = max(0.0, float(bbox[3]) - float(bbox[1]))
    except (TypeError, ValueError):
        return None
    area = width * height
    return area if area > 0 else None


def _node_area_score(node: Dict[str, Any]) -> float:
    area = _node_bbox_area(node)
    if area is None:
        return -12.0
    if area <= 0:
        return -12.0
    # Prefer visible, specific controls over full-window containers.
    if area < 25:
        return 1.0
    if area < 2_500:
        return 6.0
    if area < 80_000:
        return 4.0
    return 1.0


def node_return_priority(node: Dict[str, Any]) -> float:
    """Rank nodes for bounded payloads without changing cached element indexes."""
    score = 0.0
    source = str(node.get("source") or "").lower()
    control = _normalized_control_type(node)
    has_text = _has_textual_signal(node)

    if _has_action_signal(node):
        score += 60.0
    if has_text:
        score += 35.0
    score += _control_role_score(control, has_text)

    if source == "uia":
        score += 12.0
    elif source == "msaa":
        score += 11.0
    elif source == "vision":
        score += 10.0

    if node.get("enabled") is False:
        score -= 15.0
    if node.get("is_offscreen") is True:
        score -= 40.0
    if node.get("focused") is True or node.get("is_focused") is True:
        score += 20.0
    if node.get("selected") is True or node.get("is_selected") is True:
        score += 10.0

    confidence = node.get("confidence", node.get("ocr_confidence"))
    try:
        score += min(max(float(confidence), 0.0), 1.0) * 5.0
    except (TypeError, ValueError):
        pass

    score += _node_area_score(node)
    return score


def limit_nodes_for_response(
    nodes: List[Dict[str, Any]],
    max_nodes: Optional[int],
) -> List[Dict[str, Any]]:
    """Return highest-priority nodes for payload size control.

    ``max_nodes=None`` or ``0`` means unbounded. Sorting is stable for equal
    priorities, and element indexes are not rewritten.
    """
    if max_nodes is None or max_nodes == 0 or len(nodes) <= max_nodes:
        return list(nodes)
    ranked = sorted(
        enumerate(nodes),
        key=lambda item: (-node_return_priority(item[1]), item[0]),
    )
    return [node for _, node in ranked[:max_nodes]]


def _round_public_floats(value: Any) -> Any:
    """Round public response floats without changing internal geometry."""
    if isinstance(value, float):
        rounded = round(value, PUBLIC_FLOAT_DECIMALS)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {key: _round_public_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_public_floats(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_round_public_floats(item) for item in value)
    return value


def _compact_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """Drop empty node fields while retaining all actionable values."""
    return {
        key: value
        for key, value in node.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def project_window_state_response(
    response: Dict[str, Any],
    *,
    include_tree_markdown: bool,
    response_mode: str,
) -> Dict[str, Any]:
    """Project an internal window snapshot into a bounded public response."""
    projected = dict(response)
    if not include_tree_markdown:
        projected.pop("tree_markdown", None)
    if response_mode == "compact":
        for field in _COMPACT_WINDOW_STATE_OMIT_FIELDS:
            projected.pop(field, None)
        nodes = projected.get("nodes")
        if isinstance(nodes, list):
            projected["nodes"] = [
                _compact_node(node) if isinstance(node, dict) else node
                for node in nodes
            ]
    return _round_public_floats(projected)


def build_accessibility_hint(
    *,
    uia_quality: str,
    msaa_quality: str,
    vision_quality: str,
    uia_node_count: int,
    msaa_node_count: int,
    vision_node_count: int,
    returned_node_count: int,
    filtered_node_count: int,
    nodes_truncated: bool,
    query: Optional[str],
    force_som: bool,
) -> Dict[str, Any]:
    """Build a concise observation diagnostic for agents."""
    semantic_node_count = int(uia_node_count) + int(msaa_node_count)
    semantic_quality = _best_tree_quality(uia_quality, msaa_quality)
    semantic_tree_sparse = semantic_quality in {"empty", "poor"} or semantic_node_count < 8
    vision_used = int(vision_node_count) > 0

    reasons: List[str] = []
    recommendation = "use_semantic_nodes"
    preferred_sources: List[str] = []

    if not semantic_tree_sparse:
        reasons.append("uia_msaa_semantic_tree_available")
        preferred_sources.extend(["uia", "msaa"])
    elif vision_used:
        recommendation = "use_vision_or_ocr_nodes"
        reasons.append("semantic_tree_sparse_vision_available")
        preferred_sources.append("vision")
    else:
        recommendation = "use_query_zoom_or_force_som"
        reasons.append("semantic_tree_sparse")
        if not force_som:
            reasons.append("vision_not_run_or_no_nodes")

    if query and returned_node_count == 0:
        recommendation = "broaden_query_or_inspect_full_state"
        reasons.append("query_returned_no_nodes")
    elif nodes_truncated:
        recommendation = "narrow_with_query_or_increase_max_nodes"
        reasons.append("nodes_truncated")

    if vision_used and "vision" not in preferred_sources:
        preferred_sources.append("vision")

    return {
        "semantic_tree_sparse": semantic_tree_sparse,
        "semantic_quality": semantic_quality,
        "semantic_node_count": semantic_node_count,
        "uia_quality": uia_quality,
        "uia_node_count": int(uia_node_count),
        "msaa_quality": msaa_quality,
        "msaa_node_count": int(msaa_node_count),
        "vision_used": vision_used,
        "vision_quality": vision_quality,
        "vision_node_count": int(vision_node_count),
        "nodes_truncated": bool(nodes_truncated),
        "filtered_node_count": int(filtered_node_count),
        "returned_node_count": int(returned_node_count),
        "preferred_sources": preferred_sources,
        "recommended_next_step": recommendation,
        "reason": ";".join(reasons) if reasons else "observation_ready",
    }


def _rects_overlap(
    first: Tuple[int, int, int, int],
    second: Tuple[int, int, int, int],
) -> bool:
    return (
        min(first[2], second[2]) > max(first[0], second[0])
        and min(first[3], second[3]) > max(first[1], second[1])
    )


def list_related_transient_windows(
    pid: int,
    window_id: int,
    *,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """List visible same-process popup/owned windows above the target."""
    try:
        import sys

        if sys.platform != "win32":
            return []
        import win32con
        import win32gui
        import win32process

        target_rect = tuple(int(value) for value in win32gui.GetWindowRect(window_id))
        target_area = max(
            1,
            (target_rect[2] - target_rect[0]) * (target_rect[3] - target_rect[1]),
        )
        enumerated: List[Dict[str, Any]] = []

        def _enum_window(hwnd: int, _lparam: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            try:
                rect = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
                width = rect[2] - rect[0]
                height = rect[3] - rect[1]
                if width <= 1 or height <= 1:
                    return True
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                owner = int(win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0)
                root_owner = int(
                    win32gui.GetAncestor(hwnd, win32con.GA_ROOTOWNER) or 0
                )
                style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE))
                ex_style = int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE))
                enumerated.append(
                    {
                        "window_id": int(hwnd),
                        "pid": int(found_pid),
                        "title": win32gui.GetWindowText(hwnd) or "",
                        "class_name": win32gui.GetClassName(hwnd) or "",
                        "rect": rect,
                        "width": width,
                        "height": height,
                        "owner_window_id": owner or None,
                        "root_owner_window_id": root_owner or None,
                        "is_popup": bool(style & win32con.WS_POPUP),
                        "is_tool_window": bool(ex_style & win32con.WS_EX_TOOLWINDOW),
                        "enum_index": len(enumerated),
                    }
                )
            except Exception:
                logger.debug(
                    "Transient-window candidate inspection failed for hwnd=%s",
                    hwnd,
                    exc_info=True,
                )
            return True

        win32gui.EnumWindows(_enum_window, None)
        target = next(
            (
                candidate
                for candidate in enumerated
                if candidate["window_id"] == int(window_id)
            ),
            None,
        )
        if target is None:
            return []

        candidates: List[Dict[str, Any]] = []
        for candidate in enumerated:
            if candidate["window_id"] == int(window_id):
                continue
            if candidate["pid"] != int(pid):
                continue
            if candidate["enum_index"] >= target["enum_index"]:
                continue

            owner_related = (
                candidate["owner_window_id"] == int(window_id)
                or candidate["root_owner_window_id"] == int(window_id)
            )
            candidate_area = candidate["width"] * candidate["height"]
            overlap_related = (
                _rects_overlap(candidate["rect"], target_rect)
                and candidate_area < target_area * 0.95
            )
            if not (
                owner_related
                or candidate["is_popup"]
                or candidate["is_tool_window"]
                or overlap_related
            ):
                continue

            left, top, _, _ = candidate.pop("rect")
            candidate["x"] = left
            candidate["y"] = top
            candidate["z_index"] = (
                target["enum_index"] - candidate["enum_index"]
            )
            candidate.pop("enum_index", None)
            candidates.append(candidate)

        candidates.sort(key=lambda candidate: candidate["z_index"])
        return candidates[: max(0, int(limit))]
    except Exception:
        logger.debug("Transient-window detection failed", exc_info=True)
        return []


def detect_popover_candidates(
    pid: int,
    window_id: int,
    seen_popovers: Dict[int, set[int]],
    candidates: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Return markdown for unseen same-pid popover windows.

    ``seen_popovers`` is mutated in place to dedup across calls; pass the
    per-server-context dict so multiple FastMCP instances don't share state.
    """
    try:
        import sys

        if sys.platform != "win32":
            return ""
        seen = seen_popovers.setdefault(pid, set())
        unseen = []
        for candidate in candidates or list_related_transient_windows(pid, window_id):
            candidate_window_id = int(candidate["window_id"])
            if candidate_window_id in seen:
                continue
            seen.add(candidate_window_id)
            unseen.append(candidate)
        if not unseen:
            return ""

        text = (
            "## Popover / Overlay Windows\n\n"
            "The following windows belong to the same pid and are on the "
            "current screen but are not the requested window. They may be "
            "search popovers, context menus, or dialogs; call "
            "`get_window_state(pid={}, window_id=<id>)` to inspect:\n\n"
        ).format(pid)
        for c in unseen:
            text += '- window_id={} "{}" at ({},{}) {}x{}\n'.format(
                c["window_id"],
                c["title"] or "(no title)",
                c["x"],
                c["y"],
                c["width"],
                c["height"],
            )
        return text
    except Exception:
        logger.debug("Popover detection failed", exc_info=True)
        return ""


async def capture_related_window_screenshots(
    automation_handler: Any,
    candidates: List[Dict[str, Any]],
    *,
    max_image_dimension: int,
) -> List[Dict[str, Any]]:
    """Capture related transient windows without activating their owner."""
    if not candidates:
        return []

    from PIL import Image as PILImage

    desktop_image = None
    desktop_origin = (0, 0)
    captures: List[Dict[str, Any]] = []
    for candidate in candidates:
        child_pid = int(candidate["pid"])
        child_window_id = int(candidate["window_id"])
        bounds = {
            "x": int(candidate["x"]),
            "y": int(candidate["y"]),
            "width": int(candidate["width"]),
            "height": int(candidate["height"]),
        }
        window_state = inspect_window_capture_state(
            child_pid,
            child_window_id,
            bounds,
        )
        try:
            from computer_server.handlers.windows_wgc import capture_window_with_wgc

            wgc_capture = await capture_window_with_wgc(child_window_id)
            img, geometry = prepare_direct_window_snapshot_image(
                wgc_capture.image,
                bounds,
                max_image_dimension,
            )
            capture_metadata = build_wgc_window_capture_metadata(
                pid=child_pid,
                window_id=child_window_id,
                bounds=bounds,
                geometry=geometry,
                window_state=window_state,
                wgc_metadata=wgc_capture.metadata,
            )
        except Exception as wgc_error:
            if desktop_image is None:
                screenshot_result = await automation_handler.screenshot(
                    format="png",
                    quality=95,
                )
                if not screenshot_result.get("success"):
                    logger.debug(
                        "Transient screenshot fallback failed: %s",
                        screenshot_result.get("error", "unknown error"),
                    )
                    break
                desktop_bytes = base64.b64decode(screenshot_result["image_data"])
                desktop_image = PILImage.open(BytesIO(desktop_bytes))
                desktop_image.load()
                origin = screenshot_result.get("origin") or {}
                desktop_origin = (
                    int(origin.get("x", 0) or 0),
                    int(origin.get("y", 0) or 0),
                )
            img, geometry = prepare_window_snapshot_image(
                desktop_image.copy(),
                bounds,
                desktop_origin,
                max_image_dimension,
            )
            capture_metadata = build_window_capture_metadata(
                pid=child_pid,
                window_id=child_window_id,
                screenshot_result={"capture_metadata": {"capture_source": "pil_imagegrab"}},
                bounds=bounds,
                geometry=geometry,
                window_state=window_state,
                fallback_reason=str(wgc_error),
                fallback_backend="wgc",
            )

        capture_metadata["related_window"] = True
        screenshot_path = save_window_state_screenshot(
            img,
            child_pid,
            child_window_id,
            None,
        )
        captures.append(
            {
                "screenshot": screenshot_path,
                "pid": child_pid,
                "window_id": child_window_id,
                "title": candidate.get("title") or "",
                "z_index": int(candidate["z_index"]),
                "screen_width": geometry.screen_width,
                "screen_height": geometry.screen_height,
                "capture_metadata": capture_metadata,
                "_capture_geometry": geometry.to_zoom_metadata(),
            }
        )
    return captures


def merge_som_vision(
    img,
    pid: int,
    window_id: int,
    screen_width: int,
    screen_height: int,
    compression_ratio: float,
    crop_origin_window_x: int,
    crop_origin_window_y: int,
    cache: Any,
    uia_quality: str,
) -> Dict[str, Any]:
    """Run SOM detection and merge vision nodes into the element cache."""
    try:
        from computer_server.handlers.som_bridge import detect_sync

        vis_buf = BytesIO()
        img.save(vis_buf, format="PNG")
        vision_result = detect_sync(
            vis_buf.getvalue(),
            screen_width,
            screen_height,
            0.3,
            True,
            10000,
        )
        if vision_result is None or not vision_result.elements:
            return {"nodes": [], "vision_node_count": 0, "vision_quality": "empty"}

        z = compression_ratio
        for el in vision_result.elements:
            el.center_x = int(el.center_x * z) + crop_origin_window_x
            el.center_y = int(el.center_y * z) + crop_origin_window_y
            el.bbox = (
                int(el.bbox[0] * z) + crop_origin_window_x,
                int(el.bbox[1] * z) + crop_origin_window_y,
                int(el.bbox[2] * z) + crop_origin_window_x,
                int(el.bbox[3] * z) + crop_origin_window_y,
            )
        cache.append_vision_elements(pid, window_id, vision_result.elements)

        nodes = []
        for el in vision_result.elements:
            metadata = getattr(el, "metadata", {}) or {}
            node = {
                "element_index": el.element_index,
                "source": "vision",
                "center_x": el.center_x,
                "center_y": el.center_y,
                "bbox": list(el.bbox),
                "control_type": el.control_type,
                "name": el.name,
                "actions": el.actions,
            }
            if metadata:
                node["metadata"] = metadata
                if "vision_confidence" in metadata:
                    node["confidence"] = metadata["vision_confidence"]
                if "ocr_confidence" in metadata:
                    node["ocr_confidence"] = metadata["ocr_confidence"]
                if "icon_label" in metadata:
                    node["icon_label"] = metadata["icon_label"]
                if "raw_caption" in metadata:
                    node["raw_caption"] = metadata["raw_caption"]
                if "normalized_label" in metadata:
                    node["normalized_label"] = metadata["normalized_label"]
                if "icon_label_source" in metadata:
                    node["icon_label_source"] = metadata["icon_label_source"]
                if "icon_label_confidence" in metadata:
                    node["icon_label_confidence"] = metadata[
                        "icon_label_confidence"
                    ]
                    node["icon_label_confidence_provenance"] = metadata.get(
                        "icon_label_confidence_provenance"
                    )
            nodes.append(node)
        cache.set_quality(pid, window_id, uia_quality)
        return {
            "nodes": nodes,
            "vision_node_count": len(vision_result.elements),
            "vision_quality": classify_tree_quality(len(vision_result.elements)),
            "vision_metadata": vision_result.metadata,
        }
    except Exception as e:
        logger.warning("SOM vision detection skipped: %s", e, exc_info=True)
    return {"nodes": [], "vision_node_count": 0, "vision_quality": "empty"}


def inspect_window_capture_state(
    pid: int,
    window_id: int,
    bounds: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return best-effort window state metadata for capture diagnostics."""
    state: Dict[str, Any] = {
        "pid": pid,
        "window_id": window_id,
        "is_minimized": None,
        "is_visible": None,
        "bounds": bounds,
    }
    try:
        import sys

        if sys.platform != "win32":
            return state
        import win32gui

        hwnd = int(window_id)
        state["is_minimized"] = bool(win32gui.IsIconic(hwnd))
        state["is_visible"] = bool(win32gui.IsWindowVisible(hwnd))
        try:
            state["class_name"] = win32gui.GetClassName(hwnd)
        except Exception:
            state["class_name"] = None
        if bounds is None and win32gui.IsWindow(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            state["raw_bounds"] = {
                "x": rect[0],
                "y": rect[1],
                "width": rect[2] - rect[0],
                "height": rect[3] - rect[1],
            }
    except Exception:
        logger.debug("Window capture state inspection failed", exc_info=True)
    return state


def build_window_capture_metadata(
    *,
    pid: int,
    window_id: int,
    screenshot_result: Dict[str, Any],
    bounds: Optional[Dict[str, Any]],
    geometry: WindowSnapshotGeometry,
    window_state: Dict[str, Any],
    fallback_reason: Optional[str] = None,
    fallback_backend: Optional[str] = None,
    stage_timings: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Build explicit provenance and risk metadata for a window snapshot."""
    warnings: List[str] = []
    target_cropped = bounds is not None and bounds.get("width", 0) > 0 and bounds.get("height", 0) > 0
    if not target_cropped:
        warnings.append("target_window_bounds_unavailable")
    if window_state.get("is_minimized") is True:
        warnings.append("target_window_minimized")
    if fallback_reason:
        warnings.append("preferred_capture_backend_failed")

    capture_metadata = {
        "capture_backend": "screen_crop" if target_cropped else "screen_full_fallback",
        "capture_mode": "screen_crop" if target_cropped else "screen_full_fallback",
        "capture_source": (screenshot_result.get("capture_metadata") or {}).get(
            "capture_source",
            "pil_imagegrab",
        ),
        "target": "window",
        "pid": pid,
        "window_id": window_id,
        "target_window_cropped": target_cropped,
        "occlusion_risk": bool(target_cropped),
        "occlusion_safe": False,
        "occlusion_detection": "not_available",
        "warnings": warnings,
        "window_state": window_state,
        "stage_timings": dict(stage_timings or {}),
        "screenshot_origin": {
            "x": geometry.screenshot_origin_x,
            "y": geometry.screenshot_origin_y,
        },
        "full_screen": {
            "width": geometry.full_width,
            "height": geometry.full_height,
        },
        "crop": {
            "screen_x": geometry.crop_origin_screen_x,
            "screen_y": geometry.crop_origin_screen_y,
            "window_x": geometry.crop_origin_window_x,
            "window_y": geometry.crop_origin_window_y,
            "width": geometry.original_width,
            "height": geometry.original_height,
        },
        "output": {
            "width": geometry.screen_width,
            "height": geometry.screen_height,
            "compression_ratio": geometry.compression_ratio,
            "resized": geometry.compression_ratio != 1.0,
        },
    }
    if target_cropped:
        occlusion_sample = _sample_window_occlusion(window_id, bounds)
        capture_metadata["occlusion_sample"] = occlusion_sample
        if occlusion_sample.get("status") == "clear":
            capture_metadata["occlusion_risk"] = False
            capture_metadata["occlusion_detection"] = "sampled_top_window_points"
        elif occlusion_sample.get("status") == "possibly_occluded":
            capture_metadata["occlusion_risk"] = True
            capture_metadata["occlusion_detection"] = "sampled_top_window_points"
            warnings.append("target_window_possibly_occluded")
    if fallback_reason:
        capture_metadata["fallback"] = {
            "from_backend": fallback_backend or "unknown",
            "reason": fallback_reason,
        }
    if "virtual_screen" in screenshot_result:
        capture_metadata["virtual_screen"] = screenshot_result["virtual_screen"]
    return _finalize_capture_metadata(capture_metadata)


def build_wgc_window_capture_metadata(
    *,
    pid: int,
    window_id: int,
    bounds: Optional[Dict[str, Any]],
    geometry: WindowSnapshotGeometry,
    window_state: Dict[str, Any],
    wgc_metadata: Dict[str, Any],
    stage_timings: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Build provenance metadata for a direct WGC window capture."""
    warnings: List[str] = []
    if window_state.get("is_minimized") is True:
        warnings.append("target_window_minimized")
    if bounds is None:
        warnings.append("target_window_bounds_unavailable")

    combined_stage_timings = dict(wgc_metadata.get("stage_timings") or {})
    combined_stage_timings.update(stage_timings or {})

    return _finalize_capture_metadata({
        **wgc_metadata,
        "capture_backend": "wgc",
        "capture_mode": "wgc_window",
        "capture_source": "windows_graphics_capture",
        "target": "window",
        "pid": pid,
        "window_id": window_id,
        "target_window_cropped": False,
        "occlusion_risk": False,
        "occlusion_safe": True,
        "occlusion_detection": "backend_guaranteed_by_dwm_composition",
        "warnings": warnings,
        "window_state": window_state,
        "stage_timings": combined_stage_timings,
        "screenshot_origin": {
            "x": geometry.screenshot_origin_x,
            "y": geometry.screenshot_origin_y,
        },
        "full_screen": {
            "width": geometry.full_width,
            "height": geometry.full_height,
        },
        "crop": {
            "screen_x": geometry.crop_origin_screen_x,
            "screen_y": geometry.crop_origin_screen_y,
            "window_x": geometry.crop_origin_window_x,
            "window_y": geometry.crop_origin_window_y,
            "width": geometry.original_width,
            "height": geometry.original_height,
        },
        "output": {
            "width": geometry.screen_width,
            "height": geometry.screen_height,
            "compression_ratio": geometry.compression_ratio,
            "resized": geometry.compression_ratio != 1.0,
        },
    })


async def capture_window_snapshot_image(
    automation_handler: Any,
    pid: int,
    window_id: int,
    *,
    max_image_dimension: int = 1280,
) -> Dict[str, Any]:
    """Capture target-window pixels without walking accessibility or running SOM."""
    from PIL import Image as PILImage

    capture_started = time.monotonic()
    capture_stage_timings = {
        "window_bounds_latency": 0.0,
        "window_state_latency": 0.0,
        "wgc_latency": 0.0,
        "fallback_activation_latency": 0.0,
        "screen_capture_latency": 0.0,
        "image_decode_latency": 0.0,
        "crop_resize_latency": 0.0,
        "capture_total_latency": 0.0,
    }
    try:
        stage_started = time.monotonic()
        bounds = await automation_handler.get_window_bounds(pid, window_id)
        capture_stage_timings["window_bounds_latency"] = (
            time.monotonic() - stage_started
        )
        stage_started = time.monotonic()
        window_capture_state = inspect_window_capture_state(pid, window_id, bounds)
        capture_stage_timings["window_state_latency"] = (
            time.monotonic() - stage_started
        )
        wgc_failure_reason: Optional[str] = None

        try:
            stage_started = time.monotonic()
            from computer_server.handlers.windows_wgc import capture_window_with_wgc

            wgc_capture = await capture_window_with_wgc(int(window_id))
            capture_stage_timings["wgc_latency"] = time.monotonic() - stage_started
            stage_started = time.monotonic()
            image, geometry = prepare_direct_window_snapshot_image(
                wgc_capture.image,
                bounds or window_capture_state.get("raw_bounds"),
                max_image_dimension,
            )
            capture_stage_timings["crop_resize_latency"] = (
                time.monotonic() - stage_started
            )
            capture_stage_timings["capture_total_latency"] = (
                time.monotonic() - capture_started
            )
            capture_metadata = build_wgc_window_capture_metadata(
                pid=pid,
                window_id=window_id,
                bounds=bounds,
                geometry=geometry,
                window_state=window_capture_state,
                wgc_metadata=wgc_capture.metadata,
                stage_timings=capture_stage_timings,
            )
        except Exception as wgc_error:
            if capture_stage_timings["wgc_latency"] == 0.0:
                capture_stage_timings["wgc_latency"] = (
                    time.monotonic() - stage_started
                )
            wgc_failure_reason = str(wgc_error)
            logger.debug(
                "WGC capture unavailable; falling back to screen crop: %s",
                wgc_error,
            )

            import sys

            if sys.platform == "win32":
                stage_started = time.monotonic()
                try:
                    import win32gui as _wg

                    _wg.SetForegroundWindow(int(window_id))
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
                capture_stage_timings["fallback_activation_latency"] = (
                    time.monotonic() - stage_started
                )

            stage_started = time.monotonic()
            screenshot_result = await automation_handler.screenshot(
                format="jpeg",
                quality=85,
            )
            capture_stage_timings["screen_capture_latency"] = (
                time.monotonic() - stage_started
            )
            if not screenshot_result.get("success"):
                return {
                    "success": False,
                    "error": "Screenshot failed: "
                    + screenshot_result.get("error", "unknown"),
                }

            stage_started = time.monotonic()
            image_bytes = base64.b64decode(screenshot_result["image_data"])
            full_image = PILImage.open(BytesIO(image_bytes))
            full_image.load()
            capture_stage_timings["image_decode_latency"] = (
                time.monotonic() - stage_started
            )
            screenshot_origin = screenshot_result.get("origin") or {}
            screenshot_origin_x = int(screenshot_origin.get("x", 0) or 0)
            screenshot_origin_y = int(screenshot_origin.get("y", 0) or 0)
            stage_started = time.monotonic()
            image, geometry = prepare_window_snapshot_image(
                full_image,
                bounds,
                (screenshot_origin_x, screenshot_origin_y),
                max_image_dimension,
            )
            capture_stage_timings["crop_resize_latency"] = (
                time.monotonic() - stage_started
            )
            capture_stage_timings["capture_total_latency"] = (
                time.monotonic() - capture_started
            )
            capture_metadata = build_window_capture_metadata(
                pid=pid,
                window_id=window_id,
                screenshot_result=screenshot_result,
                bounds=bounds,
                geometry=geometry,
                window_state=window_capture_state,
                fallback_reason=wgc_failure_reason,
                fallback_backend="wgc",
                stage_timings=capture_stage_timings,
            )

        return {
            "success": True,
            "image": image,
            "geometry": geometry,
            "capture_metadata": capture_metadata,
            "window_capture_state": window_capture_state,
        }
    except Exception as exc:
        logger.exception("capture_window_snapshot_image failed")
        return {"success": False, "error": str(exc)}


def save_window_state_screenshot(
    img,
    pid: int,
    window_id: int,
    screenshot_out_file: Optional[str],
) -> str:
    """Persist the window-state screenshot and return its path."""
    if not screenshot_out_file:
        screenshots_dir = os.path.join(tempfile.gettempdir(), "cua-screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        ts = time.strftime("%H%M%S")
        screenshot_out_file = os.path.join(
            screenshots_dir,
            f"{pid}_{window_id}_{ts}.jpg",
        )
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(screenshot_out_file, format="JPEG", quality=70, optimize=True)
    return screenshot_out_file


async def get_window_state_snapshot(
    automation_handler: Any,
    pid: int,
    window_id: int,
    query: Optional[str] = None,
    screenshot_out_file: Optional[str] = None,
    max_image_dimension: int = 1280,
    force_som: bool = False,
    include_all_nodes: bool = False,
    max_nodes: Optional[int] = DEFAULT_RETURNED_NODE_LIMIT,
    zoom_metadata: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
    walk_gate: Optional[UIAWalkGate] = None,
    msaa_gate: Optional[MSAAWalkGate] = None,
    seen_popovers: Optional[Dict[int, set[int]]] = None,
) -> Dict[str, Any]:
    """Capture screenshot, UIA nodes, optional SOM nodes, and cache metadata.

    ``walk_gate`` and ``seen_popovers`` should normally be supplied from the
    owning ``ServerContext``.  When both are omitted (e.g. single-call tests
    that don't care about cross-call dedup), fresh instances are created.
    """
    from computer_server.handlers.element_cache import get_element_cache

    if walk_gate is None:
        walk_gate = UIAWalkGate()
    if msaa_gate is None:
        msaa_gate = MSAAWalkGate()
    if seen_popovers is None:
        seen_popovers = {}

    try:
        capture_result = await capture_window_snapshot_image(
            automation_handler,
            pid,
            window_id,
            max_image_dimension=max_image_dimension,
        )
        if not capture_result.get("success"):
            return capture_result
        img = capture_result["image"]
        geometry = capture_result["geometry"]
        capture_metadata = capture_result["capture_metadata"]
        window_capture_state = capture_result["window_capture_state"]

        zoom_metadata = zoom_metadata if zoom_metadata is not None else {}
        zoom_entry = geometry.to_zoom_metadata()
        zoom_entry["capture_backend"] = capture_metadata.get("capture_backend")
        zoom_metadata[(pid, window_id)] = zoom_entry

        related_window_candidates = list_related_transient_windows(pid, window_id)
        related_screenshots = await capture_related_window_screenshots(
            automation_handler,
            related_window_candidates,
            max_image_dimension=max_image_dimension,
        )

        cache = get_element_cache()
        cache.set_window_origin(
            pid,
            window_id,
            geometry.window_origin_x,
            geometry.window_origin_y,
        )

        uia_data = await collect_uia_nodes(
            pid,
            window_id,
            query,
            geometry.window_origin_x,
            geometry.window_origin_y,
            gate=walk_gate,
        )
        nodes: List[Dict[str, Any]] = list(uia_data["nodes"])
        tree_markdown: str = uia_data["tree_markdown"]
        uia_quality: str = uia_data["uia_quality"]
        vision_quality: str = "empty"
        msaa_quality: str = "empty"
        tree_quality: str = uia_data["tree_quality"]
        total_elements: int = uia_data["total_elements"]
        uia_node_count: int = uia_data["uia_node_count"]
        msaa_total_elements: int = 0
        msaa_tree_node_count: int = 0
        msaa_node_count: int = 0
        vision_node_count: int = 0
        msaa_data: Dict[str, Any] = {}

        if should_collect_msaa(uia_data, window_capture_state):
            msaa_data = await collect_msaa_nodes(
                window_id,
                query,
                geometry.window_origin_x,
                geometry.window_origin_y,
                index_start=next_element_index(nodes),
                gate=msaa_gate,
                pid=pid,
            )
            if msaa_data["nodes"]:
                nodes.extend(msaa_data["nodes"])
            msaa_text = msaa_data.get("tree_markdown") or ""
            if msaa_text:
                if tree_markdown:
                    tree_markdown += "\n\n## MSAA Fallback\n" + msaa_text
                else:
                    tree_markdown = msaa_text
            msaa_quality = msaa_data["msaa_quality"]
            msaa_total_elements = msaa_data.get("msaa_total_elements", msaa_data["total_elements"])
            msaa_tree_node_count = msaa_data.get("msaa_tree_node_count", msaa_node_count)
            msaa_node_count = msaa_data["msaa_node_count"]
            total_elements += msaa_data["total_elements"]
            if msaa_node_count > 0:
                tree_quality = _best_tree_quality(uia_quality, msaa_quality)

        cache.update(pid, window_id, build_cached_elements(nodes))
        cache.set_quality(pid, window_id, tree_quality)

        popover_candidates_text = detect_popover_candidates(
            pid,
            window_id,
            seen_popovers,
            related_window_candidates,
        )

        if force_som or tree_quality in ("poor", "empty"):
            vision_data = merge_som_vision(
                img,
                pid,
                window_id,
                geometry.screen_width,
                geometry.screen_height,
                geometry.compression_ratio,
                geometry.crop_origin_window_x,
                geometry.crop_origin_window_y,
                cache,
                tree_quality,
            )
            nodes.extend(vision_data["nodes"])
            vision_node_count = vision_data["vision_node_count"]
            total_elements += vision_node_count
            vision_quality = vision_data["vision_quality"]
            vision_metadata = vision_data.get("vision_metadata", {})
        else:
            vision_metadata = {}

        nodes = enrich_nodes_with_action_hints(nodes)
        filtered_nodes = filter_nodes_by_query(nodes, query, include_all_nodes)
        limited_nodes = limit_nodes_for_response(filtered_nodes, max_nodes)
        returned_nodes = project_nodes_to_window_resized(limited_nodes, geometry)
        nodes_truncated = len(returned_nodes) < len(filtered_nodes)
        accessibility_hint = build_accessibility_hint(
            uia_quality=uia_quality,
            msaa_quality=msaa_quality,
            vision_quality=vision_quality,
            uia_node_count=uia_node_count,
            msaa_node_count=msaa_node_count,
            vision_node_count=vision_node_count,
            returned_node_count=len(returned_nodes),
            filtered_node_count=len(filtered_nodes),
            nodes_truncated=nodes_truncated,
            query=query,
            force_som=force_som,
        )
        provider_errors = _merge_provider_errors(uia_data, msaa_data)
        semantic_degraded_reason = _semantic_degraded_reason(
            tree_quality=tree_quality,
            uia_data=uia_data,
            msaa_quality=msaa_quality,
            vision_quality=vision_quality,
            returned_node_count=len(returned_nodes),
        )
        if semantic_degraded_reason is None and msaa_data.get("degraded_reason"):
            semantic_degraded_reason = str(msaa_data["degraded_reason"])

        from computer_server.services.target_profile_service import classify_target_profile

        target_profile = classify_target_profile(
            pid=pid,
            window_id=window_id,
            class_name=window_capture_state.get("class_name"),
        )

        screenshot_field = save_window_state_screenshot(
            img,
            pid,
            window_id,
            screenshot_out_file,
        )

        response: Dict[str, Any] = {
            "success": True,
            "screenshot": screenshot_field,
            "nodes": returned_nodes,
            "node_count": len(returned_nodes),
            "tree_markdown": tree_markdown,
            "tree_quality": tree_quality,
            "uia_quality": uia_quality,
            "msaa_quality": msaa_quality,
            "vision_quality": vision_quality,
            "vision_metadata": vision_metadata,
            "accessibility_hint": accessibility_hint,
            "observation_recommendation": accessibility_hint["recommended_next_step"],
            "semantic_degraded_reason": semantic_degraded_reason,
            "provider_errors": provider_errors,
            "capture_metadata": capture_metadata,
            "capture_reliability": capture_metadata.get("capture_reliability"),
            "target_profile": target_profile.to_dict(),
            "node_sources": sorted({node.get("source", "unknown") for node in nodes}),
            "merged_node_count": len(nodes),
            "unfiltered_node_count": len(nodes),
            "filtered_node_count": len(filtered_nodes),
            "returned_node_count": len(returned_nodes),
            "nodes_filtered": bool(query and not include_all_nodes),
            "nodes_truncated": nodes_truncated,
            "node_limit": max_nodes,
            "nodes_coordinate_space": "window_resized",
            "uia_node_count": uia_node_count,
            "msaa_total_elements": msaa_total_elements,
            "msaa_tree_node_count": msaa_tree_node_count,
            "msaa_node_count": msaa_node_count,
            "vision_node_count": vision_node_count,
            "total_elements": total_elements,
            "pid": pid,
            "window_id": window_id,
            "screen_width": geometry.screen_width,
            "screen_height": geometry.screen_height,
            "screenshot_origin": {
                "x": geometry.screenshot_origin_x,
                "y": geometry.screenshot_origin_y,
            },
            "_capture_geometry": dict(zoom_entry),
            "_related_screenshots": related_screenshots,
        }
        if popover_candidates_text:
            response["popover_candidates_text"] = popover_candidates_text
        return response
    except Exception as e:
        logger.exception("get_window_state_snapshot failed")
        return {"success": False, "error": str(e)}


async def create_zoom_image(
    automation_handler: Any,
    pid: int,
    window_id: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    zoom_metadata: Dict[Tuple[int, int], Dict[str, Any]],
) -> Dict[str, Any]:
    """Create a zoomed native-resolution crop for a prior window snapshot."""
    from PIL import Image as PILImage

    from computer_server.handlers.zoom_registry import ZoomContext, get_zoom_registry

    meta = zoom_metadata.get((pid, window_id))
    if meta is None:
        return {
            "success": False,
            "error": (
                f"No screenshot metadata for pid {pid} window_id {window_id}. "
                f"Call computer_get_window_state(pid={pid}, window_id={window_id}) first."
            ),
        }

    z = meta["compression_ratio"]
    orig_w = meta["original_width"]
    orig_h = meta["original_height"]
    wx = meta.get("window_origin_x", 0)
    wy = meta.get("window_origin_y", 0)

    rx1 = int(x1 * z)
    ry1 = int(y1 * z)
    rx2 = int(x2 * z)
    ry2 = int(y2 * z)

    rx1 = max(0, min(rx1, orig_w - 1))
    ry1 = max(0, min(ry1, orig_h - 1))
    rx2 = max(rx1 + 1, min(rx2, orig_w))
    ry2 = max(ry1 + 1, min(ry2, orig_h))

    crop_w = rx2 - rx1
    crop_h = ry2 - ry1
    pad_x = int(crop_w * 0.2)
    pad_y = int(crop_h * 0.2)

    padded_x1 = max(0, rx1 - pad_x)
    padded_y1 = max(0, ry1 - pad_y)
    padded_x2 = min(orig_w, rx2 + pad_x)
    padded_y2 = min(orig_h, ry2 + pad_y)

    csx = meta.get("crop_origin_screen_x", wx)
    csy = meta.get("crop_origin_screen_y", wy)
    screen_x1 = csx + padded_x1
    screen_y1 = csy + padded_y1
    screen_x2 = csx + padded_x2
    screen_y2 = csy + padded_y2

    cropped = None
    capture_backend = "screen_crop"
    capture_metadata: Dict[str, Any] = {"capture_backend": "screen_crop"}
    wgc_failure_reason: Optional[str] = None

    if meta.get("capture_backend") == "wgc":
        try:
            from computer_server.handlers.windows_wgc import capture_window_with_wgc

            wgc_capture = await capture_window_with_wgc(int(window_id))
            img = wgc_capture.image
            current_wx = wx
            current_wy = wy
            try:
                current_bounds = await automation_handler.get_window_bounds(pid, window_id)
                current_wx = int((current_bounds or {}).get("x", wx) or wx)
                current_wy = int((current_bounds or {}).get("y", wy) or wy)
            except Exception:
                pass

            image_x1 = max(0, min(padded_x1, img.width - 1))
            image_y1 = max(0, min(padded_y1, img.height - 1))
            image_x2 = max(image_x1 + 1, min(padded_x2, img.width))
            image_y2 = max(image_y1 + 1, min(padded_y2, img.height))
            screen_x1 = current_wx + image_x1
            screen_y1 = current_wy + image_y1
            screen_x2 = current_wx + image_x2
            screen_y2 = current_wy + image_y2
            cropped = img.crop((image_x1, image_y1, image_x2, image_y2))
            capture_backend = "wgc"
            capture_metadata = {
                **wgc_capture.metadata,
                "capture_backend": "wgc",
                "occlusion_safe": True,
                "occlusion_risk": False,
            }
        except Exception as wgc_error:
            wgc_failure_reason = str(wgc_error)
            logger.debug("WGC zoom capture unavailable; falling back to screen crop: %s", wgc_error)

    if cropped is None:
        result = await automation_handler.screenshot(format="png", quality=95)
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "Screenshot failed")}

        img_bytes = base64.b64decode(result["image_data"])
        img = PILImage.open(BytesIO(img_bytes))
        screenshot_origin = result.get("origin") or {}
        screenshot_origin_x = int(screenshot_origin.get("x", 0) or 0)
        screenshot_origin_y = int(screenshot_origin.get("y", 0) or 0)

        image_x1 = screen_x1 - screenshot_origin_x
        image_y1 = screen_y1 - screenshot_origin_y
        image_x2 = screen_x2 - screenshot_origin_x
        image_y2 = screen_y2 - screenshot_origin_y
        image_x1 = max(0, min(image_x1, img.width - 1))
        image_y1 = max(0, min(image_y1, img.height - 1))
        image_x2 = max(image_x1 + 1, min(image_x2, img.width))
        image_y2 = max(image_y1 + 1, min(image_y2, img.height))
        screen_x1 = image_x1 + screenshot_origin_x
        screen_y1 = image_y1 + screenshot_origin_y
        screen_x2 = image_x2 + screenshot_origin_x
        screen_y2 = image_y2 + screenshot_origin_y
        cropped = img.crop((image_x1, image_y1, image_x2, image_y2))
        capture_metadata = {
            "capture_backend": "screen_crop",
            "occlusion_safe": False,
            "occlusion_risk": True,
        }
        if wgc_failure_reason:
            capture_metadata["fallback"] = {
                "from_backend": "wgc",
                "reason": wgc_failure_reason,
            }

    padded_w = screen_x2 - screen_x1
    padded_h = screen_y2 - screen_y1

    zoom_id = f"zoom-{uuid.uuid4().hex}"
    zoom_ctx = ZoomContext(
        zoom_id=zoom_id,
        pid=pid,
        window_id=window_id,
        crop_rect=(screen_x1, screen_y1, screen_x2, screen_y2),
        scale_x=cropped.width / padded_w if padded_w > 0 else 1.0,
        scale_y=cropped.height / padded_h if padded_h > 0 else 1.0,
        output_width=cropped.width,
        output_height=cropped.height,
        padding=max(pad_x, pad_y),
    )
    get_zoom_registry().set(pid, window_id, zoom_ctx)

    buffered = BytesIO()
    cropped.save(buffered, format="PNG", optimize=True)
    buffered.seek(0)

    return {
        "success": True,
        "image_data": base64.b64encode(buffered.getvalue()).decode(),
        "format": "png",
        "width": cropped.width,
        "height": cropped.height,
        "crop_rect": [screen_x1, screen_y1, screen_x2, screen_y2],
        "pid": pid,
        "window_id": window_id,
        "zoom_id": zoom_id,
        "capture_backend": capture_backend,
        "capture_metadata": capture_metadata,
    }
