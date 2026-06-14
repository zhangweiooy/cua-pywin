from __future__ import annotations

from computer_server.services.agent_state_service import build_agent_state


def test_build_agent_state_returns_compact_summary_by_default():
    observation = {
        "window": {
            "id": 456,
            "pid": 123,
            "app": "Weixin.exe",
            "title": "微信",
        },
        "snapshot_id": "snap-1",
        "screenshot_id": "shot-1",
        "tree_quality": "poor",
        "uia_quality": "empty",
        "msaa_quality": "poor",
        "vision_quality": "ok",
        "returned_node_count": 2,
        "filtered_node_count": 12,
        "nodes_truncated": True,
        "observation_recommendation": "narrow_with_query_or_increase_max_nodes",
        "nodes": [
            {
                "element_index": 7,
                "control_type": "Button",
                "name": "发送",
                "source": "uia",
                "action_hints": ["click"],
                "focused": True,
                "bbox": [10, 20, 40, 60],
            },
            {
                "element_index": 8,
                "type": "icon",
                "normalized_label": "more options",
                "source": "vision",
                "center_x": 100,
                "center_y": 200,
            },
        ],
    }

    agent_state = build_agent_state(observation)

    assert agent_state["text_limit"] == 500
    assert agent_state["truncated"] is False
    assert agent_state["node_sample_count"] == 2
    assert agent_state["recommended_next_step"] == "narrow_with_query_or_increase_max_nodes"
    assert "Weixin.exe pid=123 hwnd=456" in agent_state["text"]
    assert 'Title: "微信"' in agent_state["text"]
    assert '[7] Button "发送"' in agent_state["text"]
    assert "focused" in agent_state["text"]
    assert '[8] icon "more options"' in agent_state["text"]


def test_build_agent_state_can_truncate_or_return_full_text():
    observation = {
        "window": {"id": 1, "pid": 2, "title": "Dense"},
        "nodes": [
            {
                "element_index": i,
                "control_type": "Text",
                "name": f"long label {i}",
                "bbox": [i, i, i + 10, i + 20],
            }
            for i in range(30)
        ],
    }

    compact = build_agent_state(observation, text_limit=120)
    full = build_agent_state(observation, text_limit=120, show_full_text=True)

    assert compact["truncated"] is True
    assert len(compact["text"]) <= 120
    assert full["truncated"] is False
    assert len(full["text"]) > len(compact["text"])
