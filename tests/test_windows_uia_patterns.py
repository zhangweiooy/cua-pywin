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


def _tree_result(*control_types: str, quality: str = "ok"):
    nodes = [
        windows_uia.UiaNode(
            element_index=index,
            control_type=control_type,
            name=control_type,
            automation_id=None,
        )
        for index, control_type in enumerate(control_types)
    ]
    return windows_uia.UiaTreeResult(
        tree_markdown="",
        nodes=nodes,
        indexed_nodes=nodes,
        tree_quality=quality,
        total_elements=len(nodes),
    )


@pytest.mark.parametrize(
    ("control_types", "expected_quality"),
    [
        (("Button",) * 4, "poor"),
        (("Text",) * 16, "ok"),
        (("Document",) + ("Button",) * 51, "rich"),
    ],
)
def test_classify_tree_quality_uses_retained_semantic_nodes(
    control_types,
    expected_quality,
):
    nodes = _tree_result(*control_types).nodes

    assert windows_uia._classify_tree_quality(nodes) == expected_quality


def test_walk_uia_tree_retries_chromium_render_widget_until_content_is_ready(
    monkeypatch,
):
    shell = _tree_result("Button", "Button")
    loading = _tree_result("Document")
    content = _tree_result("Document", "Edit", "Button")
    calls: list[int] = []
    sleeps: list[float] = []
    clock = {"now": 0.0}

    def fake_walk(hwnd, query, max_depth, max_elements):
        calls.append(hwnd)
        if hwnd == 100:
            return shell
        return loading if calls.count(200) == 1 else content

    monkeypatch.setattr(windows_uia, "_walk_uia_tree_once", fake_walk)
    monkeypatch.setattr(
        windows_uia.win32gui,
        "GetClassName",
        lambda hwnd: (
            "Chrome_WidgetWin_1" if hwnd == 100 else "Chrome_RenderWidgetHostHWND"
        ),
    )
    monkeypatch.setattr(
        windows_uia.win32gui,
        "EnumChildWindows",
        lambda _hwnd, callback, context: callback(200, context),
    )
    monkeypatch.setattr(windows_uia.win32gui, "IsWindowVisible", lambda _hwnd: True)

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(windows_uia.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(windows_uia.time, "sleep", fake_sleep)

    result = windows_uia.walk_uia_tree(100)

    assert result is content
    assert calls == [100, 200, 200]
    assert sleeps == [0.1]


def test_walk_uia_tree_does_not_probe_ready_content_twice(monkeypatch):
    content = _tree_result("Document", "Edit", "Button")
    calls: list[int] = []

    def fake_walk(hwnd, query, max_depth, max_elements):
        calls.append(hwnd)
        return content

    monkeypatch.setattr(windows_uia, "_walk_uia_tree_once", fake_walk)
    monkeypatch.setattr(
        windows_uia,
        "_chromium_render_widget_hwnds",
        lambda _hwnd: pytest.fail("ready content must not enumerate render widgets"),
    )

    result = windows_uia.walk_uia_tree(100)

    assert result is content
    assert calls == [100]


def test_walk_uia_tree_marks_unavailable_chromium_content_as_poor(monkeypatch):
    shell = _tree_result("Button", "Button")
    calls: list[int] = []
    sleeps: list[float] = []
    clock = {"now": 0.0}

    def fake_walk(hwnd, query, max_depth, max_elements):
        calls.append(hwnd)
        return shell

    monkeypatch.setattr(windows_uia, "_walk_uia_tree_once", fake_walk)
    monkeypatch.setattr(
        windows_uia.win32gui,
        "GetClassName",
        lambda hwnd: (
            "Chrome_WidgetWin_1" if hwnd == 100 else "Chrome_RenderWidgetHostHWND"
        ),
    )
    monkeypatch.setattr(
        windows_uia.win32gui,
        "EnumChildWindows",
        lambda _hwnd, callback, context: callback(200, context),
    )
    monkeypatch.setattr(windows_uia.win32gui, "IsWindowVisible", lambda _hwnd: True)

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(windows_uia.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(windows_uia.time, "sleep", fake_sleep)

    result = windows_uia.walk_uia_tree(100)

    assert result is shell
    assert result.tree_quality == "poor"
    assert calls[0] == 100
    assert all(hwnd == 200 for hwnd in calls[1:])
    assert len(calls) == 7
    assert sum(sleeps) == pytest.approx(2.0)
    assert max(sleeps) <= 0.5
