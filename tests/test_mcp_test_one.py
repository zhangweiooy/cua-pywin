from __future__ import annotations

import io
import sys

import pytest

from scripts.mcp_test_one import _parse_tool_args


class _BinaryStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


def test_parse_tool_args_accepts_inline_stdin_and_file(tmp_path, monkeypatch) -> None:
    assert _parse_tool_args('{"active": true}') == {"active": True}

    monkeypatch.setattr(
        sys,
        "stdin",
        _BinaryStdin("\ufeff{\"action\":\"status\"}".encode("utf-8")),
    )
    assert _parse_tool_args("-") == {"action": "status"}

    args_file = tmp_path / "args.json"
    args_file.write_text("\ufeff{\"query\":\"微信\"}", encoding="utf-8")
    assert _parse_tool_args(f"@{args_file}") == {"query": "微信"}


def test_parse_tool_args_rejects_non_object() -> None:
    with pytest.raises(SystemExit):
        _parse_tool_args("[1, 2]")
