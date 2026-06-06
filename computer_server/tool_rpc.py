"""Shared JSON-RPC helpers for the local CLI HTTP surface."""

from __future__ import annotations

from typing import Any


AUTH_HEADER = "X-Cua-Token"
BATCH_HEADER = "X-Cua-Batch"
DAEMON_TOKEN_ENV = "CUA_WIN_DAEMON_TOKEN"


def tool_to_json(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        return tool

    annotations = getattr(tool, "annotations", None)
    if hasattr(annotations, "model_dump"):
        annotations = annotations.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

    data: dict[str, Any] = {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", None),
        "inputSchema": (
            getattr(tool, "parameters", None)
            or getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
        ),
    }
    output_schema = getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None)
    if output_schema is not None:
        data["outputSchema"] = output_schema
    if annotations is not None:
        data["annotations"] = annotations
    title = getattr(tool, "title", None)
    if title:
        data["title"] = title
    return {key: value for key, value in data.items() if value is not None}


def call_result_to_json(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", None)
    if content is not None:
        content = [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(item, "model_dump")
            else item
            for item in content
        ]
    return {
        "is_error": bool(getattr(result, "is_error", False)),
        "structured_content": getattr(result, "structured_content", None),
        "content": content,
    }
