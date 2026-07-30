from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from computer_server.handlers import windows_window
from computer_server.handlers.windows_window import WindowsWindowHandler


class _FakeUser32:
    def __init__(self, *, foreground: int = 101) -> None:
        self.foreground = foreground
        self.calls: list[tuple[object, ...]] = []

    def IsIconic(self, hwnd: int) -> bool:
        self.calls.append(("IsIconic", hwnd))
        return False

    def ShowWindow(self, hwnd: int, command: int) -> None:
        self.calls.append(("ShowWindow", hwnd, command))

    def GetForegroundWindow(self) -> int:
        self.calls.append(("GetForegroundWindow",))
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd: int, process_id: object) -> int:
        self.calls.append(("GetWindowThreadProcessId", hwnd, process_id))
        return {101: 201, 404: 304}[hwnd]

    def AttachThreadInput(
        self,
        source_thread: int,
        target_thread: int,
        attach: bool,
    ) -> bool:
        self.calls.append(
            ("AttachThreadInput", source_thread, target_thread, attach)
        )
        return True

    def BringWindowToTop(self, hwnd: int) -> bool:
        self.calls.append(("BringWindowToTop", hwnd))
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        self.calls.append(("SetForegroundWindow", hwnd))
        self.foreground = hwnd
        return True


class _FakeKernel32:
    def GetCurrentThreadId(self) -> int:
        return 901


def _handler() -> WindowsWindowHandler:
    handler = object.__new__(WindowsWindowHandler)
    handler._get_window_by_id = lambda window_id: object()
    return handler


@pytest.mark.asyncio
async def test_activate_window_attaches_threads_without_synthetic_alt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = _FakeUser32()
    monkeypatch.setattr(windows_window.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=user32, kernel32=_FakeKernel32()),
        raising=False,
    )

    result = await _handler().activate_window(404)

    assert result == {"success": True}
    assert not any(call[0] == "keybd_event" for call in user32.calls)
    assert ("SetForegroundWindow", 404) in user32.calls
    assert {
        call
        for call in user32.calls
        if call[0] == "AttachThreadInput" and call[3] is True
    } == {
        ("AttachThreadInput", 901, 201, True),
        ("AttachThreadInput", 901, 304, True),
    }
    assert {
        call
        for call in user32.calls
        if call[0] == "AttachThreadInput" and call[3] is False
    } == {
        ("AttachThreadInput", 901, 201, False),
        ("AttachThreadInput", 901, 304, False),
    }


@pytest.mark.asyncio
async def test_activate_window_detaches_threads_when_activation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = _FakeUser32()

    def _raise_on_foreground(hwnd: int) -> bool:
        user32.calls.append(("SetForegroundWindow", hwnd))
        raise OSError("foreground denied")

    user32.SetForegroundWindow = _raise_on_foreground
    monkeypatch.setattr(windows_window.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=user32, kernel32=_FakeKernel32()),
        raising=False,
    )

    result = await _handler().activate_window(404)

    assert result == {"success": False, "error": "foreground denied"}
    assert {
        call
        for call in user32.calls
        if call[0] == "AttachThreadInput" and call[3] is False
    } == {
        ("AttachThreadInput", 901, 201, False),
        ("AttachThreadInput", 901, 304, False),
    }
