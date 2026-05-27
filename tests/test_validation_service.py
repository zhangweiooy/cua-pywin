from __future__ import annotations

import math

import pytest

from computer_server.services.validation_service import (
    ValidationError,
    validate_click_action,
    validate_click_count,
    validate_drag_duration,
    validate_dom_attributes,
    validate_javascript,
    validate_page_action,
    validate_port,
    validate_screenshot_request,
    validate_scroll_direction,
    validate_shell,
    validate_tab_index,
    validate_zoom_rect,
)


def test_validate_screenshot_request_rejects_unknown_format():
    with pytest.raises(ValidationError):
        validate_screenshot_request("webp", 85)


def test_validate_screenshot_request_rejects_out_of_range_quality():
    with pytest.raises(ValidationError):
        validate_screenshot_request("jpeg", 0)


def test_validate_screenshot_request_accepts_jpg_alias():
    assert validate_screenshot_request("jpg", 90) == ("jpeg", 90)


def test_validate_click_count_is_one_to_three():
    assert validate_click_count(1) == 1
    assert validate_click_count(3) == 3
    with pytest.raises(ValidationError):
        validate_click_count(0)
    with pytest.raises(ValidationError):
        validate_click_count(4)


def test_validate_numeric_ranges_reject_non_finite_values():
    with pytest.raises(ValidationError):
        validate_drag_duration(math.inf)


def test_validate_enum_values_are_strict():
    assert validate_click_action("PRESS") == "press"
    assert validate_shell("cmd") == "cmd"
    assert validate_scroll_direction("down") == "down"
    assert validate_page_action("get_text") == "get_text"

    with pytest.raises(ValidationError):
        validate_shell("bash")
    with pytest.raises(ValidationError):
        validate_scroll_direction("forward")
    with pytest.raises(ValidationError):
        validate_page_action("eval")


def test_validate_zoom_rect_requires_positive_area():
    assert validate_zoom_rect(1, 2, 3, 4) == (1, 2, 3, 4)

    with pytest.raises(ValidationError):
        validate_zoom_rect(3, 2, 3, 4)
    with pytest.raises(ValidationError):
        validate_zoom_rect(1, 4, 3, 4)


def test_validate_cdp_bounds():
    assert validate_port(None) is None
    assert validate_port(9222) == 9222
    assert validate_tab_index(0) == 0

    with pytest.raises(ValidationError):
        validate_port(70000)
    with pytest.raises(ValidationError):
        validate_tab_index(-1)


def test_validate_dom_attributes_limits_html_access():
    assert validate_dom_attributes(["textContent", "href"], allow_html=False) == [
        "textContent",
        "href",
    ]

    with pytest.raises(ValidationError):
        validate_dom_attributes(["innerHTML"], allow_html=False)

    assert validate_dom_attributes(["innerHTML"], allow_html=True) == ["innerHTML"]


def test_validate_javascript_requires_explicit_allow_flag():
    with pytest.raises(ValidationError):
        validate_javascript("document.title", allow_javascript=False)

    assert validate_javascript("document.title", allow_javascript=True) == "document.title"
