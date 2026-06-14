from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


FIXTURE_SCENARIO = "fixture-basic"
LIST_TOOLS_SCENARIO = "list-tools"


def build_prompt(scenario: str, *, window_title: str) -> str:
    if scenario == LIST_TOOLS_SCENARIO:
        return (
            "Use the cua-pywin MCP tools. List the available tools, then call "
            "computer_get_screen_size. Report the tool names you used."
        )
    if scenario != FIXTURE_SCENARIO:
        raise ValueError(f"unknown scenario: {scenario}")
    return f"""Use the cua-pywin MCP tools to interact with a Windows fixture.

Target window title: {window_title}

Required workflow:
1. Find or rehydrate the target window.
2. Call computer_get_window_state on that window.
3. Use the returned agent_state/state_text and nodes to locate the Increment button.
4. Click Increment once.
5. Verify the visible counter changed with capture_after or a fresh computer_get_window_state.

Do not use raw screen coordinates unless the structured state is insufficient.
Report the tools you used and the final observed counter text."""


def _walk_json(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def extract_tool_names_from_jsonl(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        for item in _walk_json(payload):
            if not isinstance(item, dict):
                continue
            name = item.get("tool") or item.get("tool_name") or item.get("name")
            if isinstance(name, str) and name.startswith("computer_"):
                names.append(name)
    return names


def missing_required_tools(tool_names: Iterable[str], scenario: str) -> list[str]:
    required = ["computer_get_window_state"]
    if scenario == LIST_TOOLS_SCENARIO:
        required = ["computer_get_screen_size"]
    elif scenario == FIXTURE_SCENARIO:
        required.append("computer_click")
    seen = set(tool_names)
    return [name for name in required if name not in seen]


def _read_transcript(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run or validate a cua-win agent smoke scenario."
    )
    parser.add_argument(
        "--scenario",
        choices=[LIST_TOOLS_SCENARIO, FIXTURE_SCENARIO],
        default=FIXTURE_SCENARIO,
        help="Smoke scenario to run.",
    )
    parser.add_argument(
        "--window-title",
        default="CuaPywin Agent Smoke Fixture",
        help="Fixture title used in the generated prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and required tool set without launching an agent.",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        help="Validate an existing JSONL transcript instead of launching an agent.",
    )
    parser.add_argument(
        "--agent-command",
        default="codex",
        help="Agent executable to launch when not using --dry-run or --transcript.",
    )
    args = parser.parse_args()

    prompt = build_prompt(args.scenario, window_title=args.window_title)
    if args.dry_run:
        payload = {
            "scenario": args.scenario,
            "required_tools": missing_required_tools([], args.scenario),
            "prompt": prompt,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.transcript:
        transcript = _read_transcript(args.transcript)
    else:
        completed = subprocess.run(
            [args.agent_command, "exec", prompt],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        transcript = completed.stdout + "\n" + completed.stderr
        if completed.returncode != 0:
            sys.stderr.write(transcript)
            return completed.returncode

    tool_names = extract_tool_names_from_jsonl(transcript)
    missing = missing_required_tools(tool_names, args.scenario)
    result = {
        "success": not missing,
        "scenario": args.scenario,
        "tool_names": tool_names,
        "missing_required_tools": missing,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
