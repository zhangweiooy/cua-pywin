from __future__ import annotations

import asyncio
import sys
import threading

import pytest

from computer_server.handlers import windows


class _FakePyperclip:
    value = ""

    @classmethod
    def paste(cls):
        return cls.value

    @classmethod
    def copy(cls, text):
        cls.value = text


class _FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key))

    def release(self, key):
        self.events.append(("release", key))


@pytest.mark.asyncio
async def test_clipboard_set_waits_for_shared_lock(monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(windows, "_CLIPBOARD_LOCK", lock)
    monkeypatch.setitem(sys.modules, "pyperclip", _FakePyperclip)
    handler = windows.WindowsAutomationHandler()

    lock.acquire()
    task = asyncio.create_task(handler.set_clipboard("blocked"))
    try:
        await asyncio.sleep(0.05)
        assert task.done() is False
    finally:
        lock.release()

    result = await asyncio.wait_for(task, timeout=1)

    assert result == {"success": True}
    assert _FakePyperclip.value == "blocked"


@pytest.mark.asyncio
async def test_clipboard_get_uses_shared_lock(monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(windows, "_CLIPBOARD_LOCK", lock)
    monkeypatch.setitem(sys.modules, "pyperclip", _FakePyperclip)
    _FakePyperclip.value = "current"
    handler = windows.WindowsAutomationHandler()

    result = await handler.copy_to_clipboard()

    assert result == {"success": True, "content": "current"}


@pytest.mark.asyncio
async def test_type_reports_unknown_when_later_clipboard_chunk_fails(monkeypatch):
    class FailingPyperclip:
        value = "original"

        @classmethod
        def paste(cls):
            return cls.value

        @classmethod
        def copy(cls, text):
            if text == "def":
                raise RuntimeError("clipboard unavailable")
            cls.value = text

    lock = threading.Lock()
    monkeypatch.setattr(windows, "_CLIPBOARD_LOCK", lock)
    monkeypatch.setitem(sys.modules, "pyperclip", FailingPyperclip)
    handler = windows.WindowsAutomationHandler()
    handler.keyboard = _FakeKeyboard()

    result = await handler._type_text_via_clipboard("abcdef", chunk_size=3)

    assert result["success"] is False
    assert result["code"] == "ACTION_OUTCOME_UNKNOWN"
    assert FailingPyperclip.value == "original"
    assert handler.keyboard.events[:4] == [
        ("press", windows.KBKey.ctrl),
        ("press", "v"),
        ("release", "v"),
        ("release", windows.KBKey.ctrl),
    ]
