"""Lightweight Windows target profiling for action diagnostics."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TargetProfile:
    """Best-effort profile of a target window or foreground app."""

    family: str
    process_name: str = ""
    class_name: str = ""
    title: str = ""
    signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"family": self.family}
        if self.process_name:
            data["process_name"] = self.process_name
        if self.class_name:
            data["class_name"] = self.class_name
        if self.title:
            data["title"] = self.title
        if self.signals:
            data["signals"] = list(self.signals)
        return data


def classify_target_profile(
    *,
    window: Optional[Mapping[str, Any]] = None,
    pid: Optional[int] = None,
    window_id: Optional[int] = None,
    app_name: Optional[str] = None,
    process_name: Optional[str] = None,
    class_name: Optional[str] = None,
    title: Optional[str] = None,
) -> TargetProfile:
    """Classify a Windows target without affecting the caller on failure."""
    process = _first_text(
        process_name,
        _window_text(window, "process_name", "app"),
        app_name,
        _process_name_from_pid(pid),
    )
    cls = _first_text(class_name, _window_text(window, "class_name"), _class_name_from_hwnd(window_id))
    win_title = _first_text(title, _window_text(window, "title"), _title_from_hwnd(window_id))
    process_l = process.lower()
    class_l = cls.lower()
    title_l = win_title.lower()

    checks = (
        ("wechat", _is_wechat(process_l, class_l, title_l)),
        ("terminal", _is_terminal(process_l, class_l, title_l)),
        ("chromium", _is_chromium(process_l, class_l, title_l)),
        ("wpf", _is_wpf(process_l, class_l, title_l)),
        ("winui3", _is_winui3(process_l, class_l, title_l)),
        ("gtk", _is_gtk(process_l, class_l, title_l)),
        ("vcl_sal", _is_vcl_sal(process_l, class_l, title_l)),
    )
    for family, signals in checks:
        if signals:
            return TargetProfile(
                family=family,
                process_name=process,
                class_name=cls,
                title=win_title,
                signals=tuple(signals),
            )
    return TargetProfile(
        family="generic",
        process_name=process,
        class_name=cls,
        title=win_title,
    )


def assess_delivery_risk(
    profile: TargetProfile,
    *,
    action_kind: str,
    method: str,
    used_foreground: bool,
    uses_system_input: bool,
    requires_visible_target: bool,
) -> tuple[str, str]:
    """Return a compact risk code and human-readable note."""
    family = profile.family
    action = action_kind.lower()
    if method.startswith("uia_"):
        return "semantic_invoke", "UIA pattern path; does not depend on pixel foreground input."
    if family == "terminal" and action in {"type", "key", "hotkey", "press_key"}:
        return (
            "terminal_focus_sensitive",
            "Terminal hosts consume input through the focused console/ConPTY pipeline; verify the target focus.",
        )
    if family in {"chromium", "wpf", "gtk"} and action in {"click", "drag", "scroll", "mouse"}:
        return (
            "framework_mouse_sensitive",
            "This UI framework is known to ignore some background mouse messages; visible system input is expected.",
        )
    if family == "winui3" and action in {"click", "drag", "scroll", "mouse"}:
        return (
            "winui3_input_sensitive",
            "WinUI/XAML targets vary by control; prefer semantic nodes when available and verify post-action state.",
        )
    if family == "vcl_sal" and action in {"key", "hotkey", "press_key"}:
        return (
            "accelerator_state_sensitive",
            "Classic VCL/SAL accelerators may depend on real foreground key state.",
        )
    if requires_visible_target:
        return "visible_target_required", "Pixel/system input requires the target point to be visible and focused."
    if uses_system_input:
        return "focused_input_required", "System input is delivered to the current focused target."
    return "low", "No known target-specific delivery risk."


def target_profile_for_observation(
    *,
    app_name: Optional[str] = None,
    app_bundle_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> TargetProfile:
    """Profile a learning observation from lightweight app/window metadata."""
    return classify_target_profile(
        app_name=app_name,
        process_name=app_bundle_id,
        title=window_title,
    )


def _first_text(*values: Optional[Any]) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _window_text(window: Optional[Mapping[str, Any]], *names: str) -> str:
    if not isinstance(window, Mapping):
        return ""
    for name in names:
        value = window.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _process_name_from_pid(pid: Optional[int]) -> str:
    if not pid:
        return ""
    try:
        import psutil

        return psutil.Process(int(pid)).name() or ""
    except Exception:
        return ""


def _class_name_from_hwnd(window_id: Optional[int]) -> str:
    if not window_id or sys.platform != "win32":
        return ""
    try:
        import win32gui

        return win32gui.GetClassName(int(window_id)) or ""
    except Exception:
        return ""


def _title_from_hwnd(window_id: Optional[int]) -> str:
    if not window_id or sys.platform != "win32":
        return ""
    try:
        import win32gui

        return win32gui.GetWindowText(int(window_id)) or ""
    except Exception:
        return ""


def _is_wechat(process: str, class_name: str, title: str) -> list[str]:
    signals: list[str] = []
    if process in {"wechat.exe", "weixin.exe", "wechatapp.exe"} or "wechat" in process or "weixin" in process:
        signals.append("process")
    if "wechat" in title or "weixin" in title or "微信" in title:
        signals.append("title")
    if "wechat" in class_name or "weixin" in class_name:
        signals.append("class")
    return signals


def _is_terminal(process: str, class_name: str, title: str) -> list[str]:
    signals: list[str] = []
    terminal_classes = (
        "cascadia_hosting_window_class",
        "consolewindowclass",
        "mintty",
        "vim",
        "nvim",
    )
    if class_name.startswith(terminal_classes):
        signals.append("class")
    terminal_processes = {
        "windowsterminal.exe",
        "wt.exe",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "conhost.exe",
        "mintty.exe",
    }
    if process in terminal_processes:
        signals.append("process")
    if title.startswith(("windows powershell", "command prompt")):
        signals.append("title")
    return signals


def _is_chromium(process: str, class_name: str, title: str) -> list[str]:
    signals: list[str] = []
    if class_name.startswith("chrome_widgetwin") or "cef" in class_name or "chromium" in class_name:
        signals.append("class")
    chromium_processes = {
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "opera.exe",
        "vivaldi.exe",
        "electron.exe",
        "code.exe",
        "teams.exe",
        "slack.exe",
        "discord.exe",
    }
    if process in chromium_processes:
        signals.append("process")
    if "chromium" in title:
        signals.append("title")
    return signals


def _is_wpf(process: str, class_name: str, title: str) -> list[str]:
    del process, title
    return ["class"] if class_name.startswith("hwndwrapper") else []


def _is_winui3(process: str, class_name: str, title: str) -> list[str]:
    del process, title
    if class_name in {"winuidesktopwin32windowclass", "applicationframewindow"}:
        return ["class"]
    if "xaml" in class_name or "windows.ui" in class_name:
        return ["class"]
    return []


def _is_gtk(process: str, class_name: str, title: str) -> list[str]:
    del title
    signals = []
    if "gtk" in class_name or "gdk" in class_name:
        signals.append("class")
    if process in {"gimp.exe", "inkscape.exe"}:
        signals.append("process")
    return signals


def _is_vcl_sal(process: str, class_name: str, title: str) -> list[str]:
    del title
    signals = []
    if "salframe" in class_name or class_name.startswith(("tform", "tapplication")):
        signals.append("class")
    if process in {"soffice.bin", "soffice.exe", "swriter.exe", "scalc.exe"}:
        signals.append("process")
    return signals
