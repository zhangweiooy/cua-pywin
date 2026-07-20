from __future__ import annotations

import sys

import pytest

from computer_server.handlers import windows_uia


pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows UI Automation is available only on Windows",
)


class _NullUnknown:
    def __bool__(self) -> bool:
        return False

    def QueryInterface(self, _interface):
        raise AssertionError("QueryInterface must not be called on a null pointer")


class _TypedInvokePattern:
    def __init__(self) -> None:
        self.invocations = 0

    def __bool__(self) -> bool:
        return True

    def Invoke(self) -> None:
        self.invocations += 1


class _TypedValuePattern:
    def __init__(self) -> None:
        self.values: list[str] = []

    def __bool__(self) -> bool:
        return True

    def SetValue(self, value: str) -> None:
        self.values.append(value)


class _UnknownPattern:
    def __init__(self, interface_name: str, typed_pattern) -> None:
        self.interface_name = interface_name
        self.typed_pattern = typed_pattern
        self.queries: list[str] = []

    def __bool__(self) -> bool:
        return True

    def QueryInterface(self, interface):
        name = interface.__name__
        self.queries.append(name)
        if name != self.interface_name:
            raise ValueError("unsupported interface")
        return self.typed_pattern


class _Element:
    def __init__(
        self,
        *,
        patterns: dict[int, object] | None = None,
        control_type: int = 50020,
    ) -> None:
        self.patterns = patterns or {}
        self.CurrentControlType = control_type

    def GetCachedPattern(self, pattern_id: int):
        return self.patterns.get(pattern_id, _NullUnknown())

    def GetCurrentPattern(self, pattern_id: int):
        return self.patterns.get(pattern_id, _NullUnknown())


class _Walker:
    def __init__(self, parents: dict[object, object | None]) -> None:
        self.parents = parents

    def GetParentElement(self, element):
        return self.parents.get(element)


class _Automation:
    def __init__(self, element, parents: dict[object, object | None]) -> None:
        self.element = element
        self.RawViewWalker = _Walker(parents)

    def ElementFromPoint(self, _point):
        return self.element


def test_detect_actions_rejects_non_none_null_iunknown_pointers():
    element = _Element()

    assert windows_uia._detect_actions(element) == []


def test_detect_actions_queries_the_concrete_pattern_interface():
    typed = _TypedInvokePattern()
    unknown = _UnknownPattern("IUIAutomationInvokePattern", typed)
    element = _Element(
        patterns={windows_uia.UIA_InvokePatternId: unknown},
    )

    assert windows_uia._detect_actions(element) == ["invoke"]
    assert unknown.queries == ["IUIAutomationInvokePattern"]


def test_invoke_at_point_uses_nearest_actionable_ancestor(monkeypatch):
    typed = _TypedInvokePattern()
    parent = _Element(
        patterns={
            windows_uia.UIA_InvokePatternId: _UnknownPattern(
                "IUIAutomationInvokePattern",
                typed,
            )
        },
        control_type=50000,
    )
    child = _Element(control_type=50020)
    automation = _Automation(child, {child: parent, parent: None})
    dispatches: list[str] = []

    monkeypatch.setattr(windows_uia, "_co_initialize_sta", lambda: False)
    monkeypatch.setattr(windows_uia, "get_uia_instance", lambda: automation)

    windows_uia.invoke_at_point(
        10,
        20,
        on_dispatch=lambda: dispatches.append("invoke"),
    )

    assert typed.invocations == 1
    assert dispatches == ["invoke"]


def test_set_value_at_point_uses_nearest_editable_ancestor(monkeypatch):
    typed = _TypedValuePattern()
    parent = _Element(
        patterns={
            windows_uia.UIA_ValuePatternId: _UnknownPattern(
                "IUIAutomationValuePattern",
                typed,
            )
        },
        control_type=50004,
    )
    child = _Element(control_type=50020)
    automation = _Automation(child, {child: parent, parent: None})
    dispatches: list[str] = []

    monkeypatch.setattr(windows_uia, "_co_initialize_sta", lambda: False)
    monkeypatch.setattr(windows_uia, "get_uia_instance", lambda: automation)

    windows_uia.set_value_at_point(
        10,
        20,
        "中文",
        on_dispatch=lambda: dispatches.append("set_value"),
    )

    assert typed.values == ["中文"]
    assert dispatches == ["set_value"]


def test_unsupported_pattern_is_rejected_before_dispatch(monkeypatch):
    child = _Element(control_type=50020)
    window = _Element(control_type=windows_uia.UIA_WindowControlTypeId)
    automation = _Automation(child, {child: window, window: None})
    dispatches: list[str] = []

    monkeypatch.setattr(windows_uia, "_co_initialize_sta", lambda: False)
    monkeypatch.setattr(windows_uia, "get_uia_instance", lambda: automation)

    with pytest.raises(windows_uia.UIAPatternUnavailableError):
        windows_uia.invoke_at_point(
            10,
            20,
            on_dispatch=lambda: dispatches.append("invoke"),
        )

    assert dispatches == []
