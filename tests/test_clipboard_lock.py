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
