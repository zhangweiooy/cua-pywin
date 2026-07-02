from __future__ import annotations

from computer_server.services.target_profile_service import (
    assess_delivery_risk,
    classify_target_profile,
    target_profile_for_observation,
)


def test_classify_chromium_by_window_class():
    profile = classify_target_profile(
        process_name="chrome.exe",
        class_name="Chrome_WidgetWin_1",
        title="Example - Google Chrome",
    )

    assert profile.family == "chromium"
    assert "class" in profile.signals
    assert "process" in profile.signals


def test_classify_terminal_by_class_and_process():
    profile = classify_target_profile(
        process_name="WindowsTerminal.exe",
        class_name="CASCADIA_HOSTING_WINDOW_CLASS",
        title="PowerShell",
    )

    assert profile.family == "terminal"
    assert profile.to_dict()["family"] == "terminal"


def test_classify_wechat_by_chinese_title():
    profile = target_profile_for_observation(
        app_name="WeChat",
        app_bundle_id="Weixin.exe",
        window_title="微信",
    )

    assert profile.family == "wechat"


def test_delivery_risk_distinguishes_terminal_typing():
    profile = classify_target_profile(
        process_name="pwsh.exe",
        class_name="CASCADIA_HOSTING_WINDOW_CLASS",
    )

    risk, note = assess_delivery_risk(
        profile,
        action_kind="type",
        method="clipboard_type",
        used_foreground=True,
        uses_system_input=True,
        requires_visible_target=False,
    )

    assert risk == "terminal_focus_sensitive"
    assert "Terminal" in note
