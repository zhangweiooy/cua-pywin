"""Run controlled and live-window Windows.Graphics.Capture smoke tests."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import time
from typing import Any

from PIL import ImageChops

from computer_server.handlers.windows_wgc import capture_window_with_wgc


def _set_per_monitor_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def _root_hwnd(widget: Any) -> int:
    hwnd = int(widget.winfo_id())
    try:
        return int(ctypes.windll.user32.GetAncestor(hwnd, 2))
    except Exception:
        return hwnd


def _window_dpi(hwnd: int) -> int | None:
    try:
        return int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
    except Exception:
        return None


async def _capture_case(name: str, hwnd: int) -> tuple[dict[str, Any], Any | None]:
    started = time.monotonic()
    try:
        capture = await capture_window_with_wgc(hwnd)
        return (
            {
                "name": name,
                "success": True,
                "window_id": hwnd,
                "dpi": _window_dpi(hwnd),
                "image_size": {
                    "width": capture.image.width,
                    "height": capture.image.height,
                },
                "elapsed": time.monotonic() - started,
                "capture_metadata": capture.metadata,
            },
            capture.image,
        )
    except Exception as exc:
        return (
            {
                "name": name,
                "success": False,
                "window_id": hwnd,
                "dpi": _window_dpi(hwnd),
                "elapsed": time.monotonic() - started,
                "error": str(exc),
            },
            None,
        )


async def _run_controlled_scenarios() -> dict[str, Any]:
    import tkinter as tk

    from screeninfo import get_monitors

    root = tk.Tk()
    root.title("cua-pywin WGC target")
    root.geometry("640x420+80+80")
    root.configure(bg="#1167b1")
    canvas = tk.Canvas(root, width=640, height=420, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_rectangle(30, 30, 610, 390, fill="#f3f4f6", outline="#111827", width=4)
    canvas.create_oval(80, 80, 260, 260, fill="#facc15", outline="#111827", width=4)
    canvas.create_rectangle(330, 90, 560, 250, fill="#22c55e", outline="#111827", width=4)
    canvas.create_text(320, 330, text="WGC OCCLUSION TEST", fill="#111827", font=("Segoe UI", 24, "bold"))
    root.update()
    hwnd = _root_hwnd(root)

    results: list[dict[str, Any]] = []
    try:
        baseline, baseline_image = await _capture_case("baseline", hwnd)
        results.append(baseline)

        occluder = tk.Toplevel(root)
        occluder.title("cua-pywin WGC occluder")
        occluder.geometry("640x420+80+80")
        occluder.configure(bg="#dc2626")
        occluder.attributes("-topmost", True)
        occluder.update()
        await asyncio.sleep(0.15)
        occluded, occluded_image = await _capture_case("occluded", hwnd)
        if baseline_image is not None and occluded_image is not None:
            difference = ImageChops.difference(
                baseline_image.convert("RGB"),
                occluded_image.convert("RGB"),
            )
            occluded["matches_baseline"] = difference.getbbox() is None
        results.append(occluded)
        occluder.destroy()
        root.update()

        root.iconify()
        root.update()
        await asyncio.sleep(0.1)
        minimized, _ = await _capture_case("minimized", hwnd)
        minimized["expected_error"] = "target_window_minimized"
        minimized["passed"] = (
            not minimized["success"]
            and minimized.get("error") == minimized["expected_error"]
        )
        results.append(minimized)
        root.deiconify()
        root.update()

        monitor_results: list[dict[str, Any]] = []
        user32 = ctypes.windll.user32
        for index, monitor in enumerate(get_monitors()):
            width = min(640, max(320, int(monitor.width) - 80))
            height = min(420, max(240, int(monitor.height) - 80))
            x = int(monitor.x) + 40
            y = int(monitor.y) + 40
            user32.SetWindowPos(hwnd, 0, x, y, width, height, 0x0014)
            root.update()
            await asyncio.sleep(0.1)
            captured, _ = await _capture_case(f"monitor_{index}", hwnd)
            captured["monitor"] = {
                "index": index,
                "x": int(monitor.x),
                "y": int(monitor.y),
                "width": int(monitor.width),
                "height": int(monitor.height),
                "primary": bool(getattr(monitor, "is_primary", False)),
            }
            monitor_results.append(captured)
        results.extend(monitor_results)
    finally:
        root.destroy()

    return {"controlled_scenarios": results}


def _find_live_windows(app_filters: list[str]) -> list[dict[str, Any]]:
    import psutil
    import win32gui
    import win32process

    filters = [item.casefold() for item in app_filters]
    windows: list[dict[str, Any]] = []

    def callback(hwnd: int, _extra: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = psutil.Process(pid).name()
        except Exception:
            return True
        haystack = f"{process_name} {title}".casefold()
        if filters and not any(item in haystack for item in filters):
            return True
        windows.append(
            {
                "window_id": int(hwnd),
                "pid": int(pid),
                "process_name": process_name,
                "title": title,
            }
        )
        return True

    win32gui.EnumWindows(callback, None)
    return windows


async def _run_live_windows(app_filters: list[str], repeat: int) -> dict[str, Any]:
    windows = _find_live_windows(app_filters)
    results: list[dict[str, Any]] = []
    for window in windows:
        samples = []
        for sample_index in range(repeat):
            sample, _ = await _capture_case(
                f"{window['process_name']}:{sample_index + 1}",
                window["window_id"],
            )
            samples.append(sample)
        results.append({**window, "samples": samples})
    return {
        "requested_apps": app_filters,
        "matched_window_count": len(windows),
        "live_windows": results,
    }


async def _main_async(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if not args.live_only:
        report.update(await _run_controlled_scenarios())
    if args.app:
        report.update(await _run_live_windows(args.app, args.repeat))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate WGC occlusion/minimized/multi-monitor behavior and optionally "
            "sample real application windows."
        )
    )
    parser.add_argument(
        "--app",
        action="append",
        default=[],
        help="Process or title substring to sample; repeat for multiple applications.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="Capture count per matched live window (default: 3).",
    )
    parser.add_argument(
        "--live-only",
        action="store_true",
        help="Skip the controlled occlusion/minimized/multi-monitor scenarios.",
    )
    args = parser.parse_args()
    if args.repeat < 1 or args.repeat > 20:
        parser.error("--repeat must be between 1 and 20")

    _set_per_monitor_dpi_awareness()
    print(json.dumps(asyncio.run(_main_async(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
