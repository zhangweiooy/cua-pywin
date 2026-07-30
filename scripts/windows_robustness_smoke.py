#!/usr/bin/env python3
"""Run a read-only Windows robustness smoke across real applications.

The script exercises the current workspace implementation through the in-process
MCP server and prints a compact JSON summary. It intentionally avoids returning
screenshots, node text, or tree markdown so results are safe to paste into issue
reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

os.environ.setdefault("CUA_SKIP_SOM_WARMUP", "1")

from computer_server.mcp_server import create_mcp_server


TARGETS: dict[str, dict[str, Any]] = {
    "notepad": {
        "launch": ["notepad"],
        "query": "Notepad",
    },
    "edge": {
        "launch": ["msedge", "chrome"],
        "query": "Edge Chrome",
    },
    "terminal": {
        "launch": ["wt", "powershell"],
        "query": "Windows Terminal PowerShell",
    },
    "wechat": {
        "launch": ["微信", "WeChat", "Weixin"],
        "query": "微信 WeChat Weixin",
    },
    "dingtalk": {
        "launch": ["钉钉", "DingTalk", "DingDing"],
        "query": "钉钉 DingTalk DingDing",
    },
}


def _structured(result: Any) -> dict[str, Any]:
    envelope = getattr(result, "structured_content", None)
    if not isinstance(envelope, dict):
        return {
            "success": False,
            "error": "tool returned no structured protocol result",
        }
    if envelope.get("protocol_version") != "0.2":
        return {
            "success": False,
            "error": "tool returned an unsupported protocol result",
        }
    if envelope.get("success") is True:
        data = envelope.get("data")
        return {
            "success": True,
            **(data if isinstance(data, dict) else {}),
        }
    error = envelope.get("error")
    return {
        "success": False,
        "error": (
            error.get("message")
            if isinstance(error, dict)
            else "tool call failed"
        ),
        "code": error.get("code") if isinstance(error, dict) else None,
    }


async def _call(mcp: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return _structured(await mcp.call_tool(tool, args))


async def _first_window_by_query(mcp: Any, query: str) -> dict[str, Any] | None:
    data = await _call(mcp, "computer_list_windows", {"query": query})
    for window_record in data.get("windows", []) or []:
        if isinstance(window_record.get("window"), dict):
            return window_record["window"]
    return None


async def _launch_or_find(
    mcp: Any,
    *,
    launch_names: list[str],
    query: str,
    no_launch: bool,
    wait_timeout: int,
) -> tuple[dict[str, Any] | None, str]:
    window = await _first_window_by_query(mcp, query)
    if window:
        return window, "existing"
    if no_launch:
        return None, "unavailable:not_found"

    last_error = ""
    for app in launch_names:
        data = await _call(
            mcp,
            "computer_launch_app",
            {
                "app": app,
                "wait_for_window": True,
                "wait_timeout": wait_timeout,
            },
        )
        if isinstance(data.get("window"), dict):
            return data["window"], f"launched:{app}"
        last_error = (
            data.get("error")
            or data.get("message")
            or data.get("launch_error")
            or last_error
        )

    window = await _first_window_by_query(mcp, query)
    if window:
        return window, "found_after_launch"
    return None, f"unavailable:{last_error or 'no matching window'}"


def _summarize_state(label: str, state: dict[str, Any], source: str) -> dict[str, Any]:
    capture = state.get("capture") or {}
    metadata = capture.get("capture_metadata") or {}
    profile = state.get("target_profile") or {}
    return {
        "label": label,
        "success": bool(state.get("success")),
        "window_ready": True,
        "source": source,
        "target_family": profile.get("family"),
        "process_name": profile.get("process_name"),
        "class_name": profile.get("class_name"),
        "capture_backend": metadata.get("capture_backend"),
        "capture_reliability": state.get("capture_reliability")
        or metadata.get("capture_reliability"),
        "capture_degraded_reason": metadata.get("degraded_reason"),
        "semantic_degraded_reason": state.get("semantic_degraded_reason"),
        "tree_quality": state.get("tree_quality"),
        "uia_quality": state.get("uia_quality"),
        "msaa_quality": state.get("msaa_quality"),
        "vision_quality": state.get("vision_quality"),
        "node_count": state.get("node_count"),
        "provider_error_keys": sorted((state.get("provider_errors") or {}).keys()),
        "warning_count": len(metadata.get("warnings") or []),
    }


async def _observe_target(
    mcp: Any,
    label: str,
    spec: dict[str, Any],
    *,
    no_launch: bool,
    wait_timeout: int,
    max_nodes: int,
    max_image_dimension: int,
) -> dict[str, Any]:
    window, source = await _launch_or_find(
        mcp,
        launch_names=list(spec["launch"]),
        query=str(spec["query"]),
        no_launch=no_launch,
        wait_timeout=wait_timeout,
    )
    if not window:
        return {
            "label": label,
            "success": False,
            "window_ready": False,
            "source": source,
        }

    state = await _call(
        mcp,
        "computer_get_window_state",
        {
            "window": window,
            "max_image_dimension": max_image_dimension,
            "max_nodes": max_nodes,
            "agent_text_limit": 0,
        },
    )
    if not state.get("success"):
        return {
            "label": label,
            "success": False,
            "window_ready": True,
            "source": source,
            "error": state.get("error"),
        }
    return _summarize_state(label, state, source)


def _parse_targets(raw_targets: str) -> list[str]:
    targets = [part.strip().lower() for part in raw_targets.split(",") if part.strip()]
    unknown = [target for target in targets if target not in TARGETS]
    if unknown:
        known = ", ".join(sorted(TARGETS))
        raise SystemExit(f"unknown target(s): {', '.join(unknown)}; known: {known}")
    return targets


async def _amain(args: argparse.Namespace) -> int:
    mcp = create_mcp_server()
    summaries: list[dict[str, Any]] = []

    screen = await _call(mcp, "computer_get_screen_size", {})
    summaries.append(
        {
            "label": "screen",
            "success": "width" in screen and "height" in screen,
            "size": {"width": screen.get("width"), "height": screen.get("height")},
        }
    )

    for label in args.targets:
        try:
            summaries.append(
                await _observe_target(
                    mcp,
                    label,
                    TARGETS[label],
                    no_launch=args.no_launch,
                    wait_timeout=args.wait_timeout,
                    max_nodes=args.max_nodes,
                    max_image_dimension=args.max_image_dimension,
                )
            )
        except Exception as exc:
            summaries.append({"label": label, "success": False, "error": str(exc)})

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    if args.require_all and any(not item.get("success") for item in summaries):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets",
        default="notepad,edge,terminal,wechat,dingtalk",
        help="Comma-separated target names. Known: %s" % ", ".join(sorted(TARGETS)),
    )
    parser.add_argument("--no-launch", action="store_true", help="Only observe existing windows.")
    parser.add_argument("--require-all", action="store_true", help="Exit non-zero if any target fails.")
    parser.add_argument("--wait-timeout", type=int, default=8)
    parser.add_argument("--max-nodes", type=int, default=20)
    parser.add_argument("--max-image-dimension", type=int, default=900)
    args = parser.parse_args()
    args.targets = _parse_targets(args.targets)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
