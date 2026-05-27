"""Window-state screenshot geometry helpers."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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


def classify_tree_quality(count: int) -> str:
    """Classify element count as tree quality: empty/poor/ok/rich."""
    if count == 0:
        return "empty"
    if count > 50:
        return "rich"
    if count > 15:
        return "ok"
    return "poor"


class UIAWalkGate:
    """Per-window gate that prevents stacked UIA walks on hung providers."""

    def __init__(self, unhealthy_cooldown_s: float = 30.0) -> None:
        self._lock = threading.Lock()
        self._inflight: set[Tuple[int, int]] = set()
        self._unhealthy_until: Dict[Tuple[int, int], float] = {}
        self._unhealthy_cooldown_s = unhealthy_cooldown_s

    @property
    def unhealthy_cooldown_s(self) -> float:
        return self._unhealthy_cooldown_s

    def claim(self, pid: int, window_id: int) -> tuple[Tuple[int, int], bool, Optional[str]]:
        """Try to claim the UIA walk slot for a window."""
        gate_key = (int(pid), int(window_id))
        now = time.monotonic()
        with self._lock:
            unhealthy_until = self._unhealthy_until.get(gate_key, 0.0)
            if unhealthy_until > now:
                return (
                    gate_key,
                    False,
                    "UIA walk skipped; window is in unhealthy cooldown "
                    "(%.1fs remaining); falling back to vision." % (unhealthy_until - now),
                )
            if gate_key in self._inflight:
                return (
                    gate_key,
                    False,
                    "UIA walk skipped; another walk is already in flight "
                    "for this window; falling back to vision.",
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
                "depth": n.depth,
            })

        return {
            "nodes": nodes,
            "total_elements": uia_result.total_elements,
            "uia_node_count": len(uia_result.indexed_nodes),
            "tree_markdown": uia_result.tree_markdown,
            "uia_quality": uia_result.tree_quality,
            "tree_quality": uia_result.tree_quality,
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
        }
    finally:
        if claimed_gate:
            gate.release(gate_key)


def build_cached_elements(nodes: List[Dict[str, Any]]) -> list:
    """Build ElementCache entries from window-state node dictionaries."""
    from computer_server.handlers.element_cache import CachedElement

    cached_elements = []
    for i, node in enumerate(nodes):
        bbox = node.get("bbox") or (0, 0, 0, 0)
        cached_elements.append(CachedElement(
            element_index=node.get("element_index", i),
            source="uia",
            center_x=node.get("center_x", (bbox[0] + bbox[2]) // 2),
            center_y=node.get("center_y", (bbox[1] + bbox[3]) // 2),
            bbox=bbox if len(bbox) == 4 else (0, 0, 0, 0),
            control_type=node.get("control_type") or node.get("role"),
            name=node.get("name") or node.get("title"),
            value=node.get("value"),
            actions=node.get("actions", []),
            platform_ref=None,
            enabled=node.get("enabled", True),
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
    ]
    actions = node.get("actions")
    if isinstance(actions, list):
        searchable.extend(str(action) for action in actions)
    elif actions is not None:
        searchable.append(actions)

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


def detect_popover_candidates(
    pid: int,
    window_id: int,
    seen_popovers: Dict[int, set[int]],
) -> str:
    """Return markdown for unseen same-pid popover windows.

    ``seen_popovers`` is mutated in place to dedup across calls; pass the
    per-server-context dict so multiple FastMCP instances don't share state.
    """
    try:
        import sys

        if sys.platform != "win32":
            return ""
        import win32gui
        import win32process

        seen = seen_popovers.setdefault(pid, set())
        candidates: List[Dict[str, Any]] = []

        def _enum_popover(hwnd: int, _lparam: Any) -> bool:
            if hwnd == window_id or not win32gui.IsWindowVisible(hwnd):
                return True
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid != pid:
                return True
            r = win32gui.GetWindowRect(hwnd)
            w, h = r[2] - r[0], r[3] - r[1]
            if w <= 1 or h <= 1:
                return True
            if hwnd in seen:
                return True
            seen.add(hwnd)
            candidates.append({
                "window_id": hwnd,
                "title": win32gui.GetWindowText(hwnd),
                "x": r[0],
                "y": r[1],
                "width": w,
                "height": h,
            })
            return True

        win32gui.EnumWindows(_enum_popover, None)
        if not candidates:
            return ""

        text = (
            "## Popover / Overlay Windows\n\n"
            "The following windows belong to the same pid and are on the "
            "current screen but are not the requested window. They may be "
            "search popovers, context menus, or dialogs; call "
            "`get_window_state(pid={}, window_id=<id>)` to inspect:\n\n"
        ).format(pid)
        for c in candidates:
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
            nodes.append({
                "element_index": el.element_index,
                "source": "vision",
                "center_x": el.center_x,
                "center_y": el.center_y,
                "bbox": list(el.bbox),
                "control_type": el.control_type,
                "name": el.name,
                "actions": el.actions,
            })
        cache.set_quality(pid, window_id, uia_quality)
        return {
            "nodes": nodes,
            "vision_node_count": len(vision_result.elements),
            "vision_quality": classify_tree_quality(len(vision_result.elements)),
        }
    except Exception as e:
        logger.warning("SOM vision detection skipped: %s", e, exc_info=True)
        return {"nodes": [], "vision_node_count": 0, "vision_quality": "empty"}


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
    zoom_metadata: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
    walk_gate: Optional[UIAWalkGate] = None,
    seen_popovers: Optional[Dict[int, set[int]]] = None,
) -> Dict[str, Any]:
    """Capture screenshot, UIA nodes, optional SOM nodes, and cache metadata.

    ``walk_gate`` and ``seen_popovers`` should normally be supplied from the
    owning ``ServerContext``.  When both are omitted (e.g. single-call tests
    that don't care about cross-call dedup), fresh instances are created.
    """
    from PIL import Image as PILImage

    from computer_server.handlers.element_cache import get_element_cache

    if walk_gate is None:
        walk_gate = UIAWalkGate()
    if seen_popovers is None:
        seen_popovers = {}

    try:
        import sys

        if sys.platform == "win32":
            try:
                import win32gui as _wg

                _wg.SetForegroundWindow(int(window_id))
                await asyncio.sleep(0.05)
            except Exception:
                pass

        result = await automation_handler.screenshot(format="jpeg", quality=85)
        if not result.get("success"):
            return {
                "success": False,
                "error": "Screenshot failed: " + result.get("error", "unknown"),
            }

        img_bytes = base64.b64decode(result["image_data"])
        img = PILImage.open(BytesIO(img_bytes))
        screenshot_origin = result.get("origin") or {}
        screenshot_origin_x = int(screenshot_origin.get("x", 0) or 0)
        screenshot_origin_y = int(screenshot_origin.get("y", 0) or 0)

        bounds = await automation_handler.get_window_bounds(pid, window_id)
        img, geometry = prepare_window_snapshot_image(
            img,
            bounds,
            (screenshot_origin_x, screenshot_origin_y),
            max_image_dimension,
        )

        zoom_metadata = zoom_metadata if zoom_metadata is not None else {}
        zoom_metadata[(pid, window_id)] = geometry.to_zoom_metadata()

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
        tree_quality: str = uia_data["tree_quality"]
        total_elements: int = uia_data["total_elements"]
        uia_node_count: int = uia_data["uia_node_count"]
        vision_node_count: int = 0

        cache.update(pid, window_id, build_cached_elements(nodes))
        cache.set_quality(pid, window_id, tree_quality)

        popover_candidates_text = detect_popover_candidates(
            pid, window_id, seen_popovers
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
                uia_quality,
            )
            nodes.extend(vision_data["nodes"])
            vision_node_count = vision_data["vision_node_count"]
            total_elements += vision_node_count
            vision_quality = vision_data["vision_quality"]

        returned_nodes = filter_nodes_by_query(nodes, query, include_all_nodes)

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
            "vision_quality": vision_quality,
            "merged_node_count": len(nodes),
            "unfiltered_node_count": len(nodes),
            "returned_node_count": len(returned_nodes),
            "nodes_filtered": bool(query and not include_all_nodes),
            "uia_node_count": uia_node_count,
            "vision_node_count": vision_node_count,
            "total_elements": total_elements,
            "pid": pid,
            "window_id": window_id,
            "screen_width": geometry.screen_width,
            "screen_height": geometry.screen_height,
            "screenshot_origin": {
                "x": screenshot_origin_x,
                "y": screenshot_origin_y,
            },
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
    padded_w = screen_x2 - screen_x1
    padded_h = screen_y2 - screen_y1

    zoom_ctx = ZoomContext(
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
    }
