from __future__ import annotations

import ctypes
import sys
import threading
from types import ModuleType, SimpleNamespace

from computer_server.learning import platform
from computer_server.learning.recorder import LearningRecorder
from computer_server.learning.session import LearningError


def test_rpc_e_changed_mode_is_normalized_and_rejected(monkeypatch) -> None:
    signed_changed_mode = ctypes.c_int32(platform._RPC_E_CHANGED_MODE).value

    assert platform._normalize_hresult(signed_changed_mode) == (
        platform._RPC_E_CHANGED_MODE
    )

    monkeypatch.setattr(
        platform,
        "_co_initialize_ex_sta",
        lambda: signed_changed_mode,
    )

    assert platform._initialize_com_sta() is False


def test_uia_enricher_uses_one_sta_thread_and_balances_com(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    main_thread_id = threading.get_ident()

    def initialize() -> bool:
        calls.append(("initialize", threading.get_ident()))
        return True

    def uninitialize() -> None:
        calls.append(("uninitialize", threading.get_ident()))

    focused = SimpleNamespace(
        ControlType="EditControl",
        Name="Editor",
        AutomationId="editor",
        IsPassword=False,
        LocalizedControlType="edit",
        IsValuePatternAvailable=lambda: True,
        GetValuePattern=lambda: SimpleNamespace(Value="hello"),
    )
    fake_uia = ModuleType("uiautomation")

    def get_root_control():
        calls.append(("root", threading.get_ident()))
        return object()

    def get_focused_control():
        calls.append(("focused", threading.get_ident()))
        return focused

    fake_uia.GetRootControl = get_root_control
    fake_uia.GetFocusedControl = get_focused_control

    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "_initialize_com_sta", initialize)
    monkeypatch.setattr(platform, "_co_uninitialize", uninitialize)

    enricher = platform.UIAEnricher(uia_module=fake_uia)
    assert enricher.health_check() is True
    assert enricher.focused_element_context() is not None
    assert enricher.read_focused_value() == "hello"
    enricher.close()

    thread_ids = {thread_id for _name, thread_id in calls}
    assert len(thread_ids) == 1
    assert thread_ids != {main_thread_id}
    assert [name for name, _thread_id in calls].count("initialize") == 1
    assert [name for name, _thread_id in calls].count("uninitialize") == 1


def test_changed_mode_prevents_uia_probe_and_uninitialize(monkeypatch) -> None:
    signed_changed_mode = ctypes.c_int32(platform._RPC_E_CHANGED_MODE).value
    uninitialize_count = 0
    fake_uia = ModuleType("uiautomation")

    def unexpected_root_control():
        raise AssertionError("UIA must not run when STA initialization fails")

    def uninitialize() -> None:
        nonlocal uninitialize_count
        uninitialize_count += 1

    fake_uia.GetRootControl = unexpected_root_control
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(
        platform,
        "_co_initialize_ex_sta",
        lambda: signed_changed_mode,
    )
    monkeypatch.setattr(platform, "_co_uninitialize", uninitialize)

    enricher = platform.UIAEnricher(uia_module=fake_uia)
    assert enricher.health_check() is False
    enricher.close()

    assert uninitialize_count == 0


def test_health_check_converts_worker_start_exception_to_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    enricher = platform.UIAEnricher(uia_module=SimpleNamespace())

    def fail_start() -> bool:
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(enricher._worker, "start", fail_start)

    assert enricher.health_check() is False
    enricher.close()


def test_health_check_uses_cold_start_timeout(monkeypatch) -> None:
    observed_timeouts: list[float] = []
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    enricher = platform.UIAEnricher(uia_module=SimpleNamespace())
    monkeypatch.setattr(enricher._worker, "start", lambda: True)

    def call(_callback, timeout: float):
        observed_timeouts.append(timeout)
        return True

    monkeypatch.setattr(enricher._worker, "call", call)

    assert enricher.health_check() is True
    assert observed_timeouts == [platform._UIA_WORKER_HEALTH_TIMEOUT_SECONDS]
    assert platform._UIA_WORKER_HEALTH_TIMEOUT_SECONDS > (
        platform._UIA_WORKER_CALL_TIMEOUT_SECONDS
    )
    enricher.close()


def test_preload_uiautomation_matches_mta_and_restores_flags(monkeypatch) -> None:
    fake_uia = ModuleType("uiautomation")
    observed_flags: list[int] = []
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "_uiautomation_module", None)
    monkeypatch.setattr(platform, "_uia_import_coinit_flags", lambda: 0)
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)

    def import_module(name: str):
        assert name == "uiautomation"
        observed_flags.append(sys.coinit_flags)
        return fake_uia

    monkeypatch.setattr(platform.importlib, "import_module", import_module)

    assert platform.preload_uiautomation() is fake_uia
    assert observed_flags == [0]
    assert sys.coinit_flags == 2
    assert platform.preload_uiautomation() is fake_uia
    assert observed_flags == [0]


def test_uia_import_flags_follow_current_apartment(monkeypatch) -> None:
    monkeypatch.setattr(
        platform,
        "_current_com_apartment_type",
        lambda: platform._APTTYPE_MTA,
    )
    assert platform._uia_import_coinit_flags() == 0

    monkeypatch.setattr(
        platform,
        "_current_com_apartment_type",
        lambda: platform._APTTYPE_STA,
    )
    assert platform._uia_import_coinit_flags() == (
        platform._COINIT_APARTMENTTHREADED
    )


def test_learning_start_cleans_up_when_uia_health_check_raises(monkeypatch) -> None:
    created: list[object] = []

    class BrokenUIA:
        closed = False

        def __init__(self) -> None:
            created.append(self)

        def health_check(self) -> bool:
            raise RuntimeError("cannot start new thread")

        def close(self) -> None:
            self.closed = True

    class FakeAppDetector:
        pass

    class UnexpectedInput:
        def __init__(self) -> None:
            raise AssertionError("input hooks must not start without UIA")

    recorder = LearningRecorder()

    def init_platform() -> None:
        recorder._uia_cls = BrokenUIA
        recorder._app_detector_cls = FakeAppDetector
        recorder._input_cls = UnexpectedInput

    monkeypatch.setattr(recorder, "_init_platform", init_platform)
    monkeypatch.setattr(
        platform.VisionEnricher,
        "_ensure_ocr",
        lambda self: None,
    )

    assert recorder.start() is LearningError.UIA_UNAVAILABLE
    assert recorder.is_recording is False
    assert len(created) == 1
    assert created[0].closed is True
    assert recorder._uia is None


def test_learning_recorder_cleanup_closes_uia_worker() -> None:
    class FakeUIA:
        closed = False

        def close(self) -> None:
            self.closed = True

    recorder = LearningRecorder()
    uia = FakeUIA()
    recorder._uia = uia

    recorder._cleanup()

    assert uia.closed is True
    assert recorder._uia is None
