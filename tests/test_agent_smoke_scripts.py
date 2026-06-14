from __future__ import annotations

from scripts.run_agent_smoke import (
    build_prompt,
    extract_tool_names_from_jsonl,
    missing_required_tools,
)


def test_fixture_prompt_names_agent_state_workflow():
    prompt = build_prompt("fixture-basic", window_title="Fixture Title")

    assert "Fixture Title" in prompt
    assert "computer_get_window_state" in prompt
    assert "agent_state/state_text" in prompt
    assert "Increment" in prompt


def test_extract_tool_names_from_jsonl_handles_nested_events():
    transcript = """
{"type":"tool_call","name":"computer_get_window_state","arguments":{}}
{"event":{"tool":"computer_click","arguments":{"x":1,"y":2}}}
not-json
{"name":"not_a_computer_tool"}
"""

    assert extract_tool_names_from_jsonl(transcript) == [
        "computer_get_window_state",
        "computer_click",
    ]


def test_fixture_required_tools():
    assert missing_required_tools(["computer_get_window_state"], "fixture-basic") == [
        "computer_click"
    ]
    assert (
        missing_required_tools(
            ["computer_get_window_state", "computer_click"],
            "fixture-basic",
        )
        == []
    )
