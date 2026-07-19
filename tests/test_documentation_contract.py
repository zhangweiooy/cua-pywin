from __future__ import annotations

import json
import re
from pathlib import Path

from computer_server import __version__


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PROTOCOL_DOCS = (
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "BETA_HANDOFF.md",
    ROOT / "RELEASE.md",
    ROOT / "skills" / "cua-win" / "SKILL.md",
    ROOT / "skills" / "cua-win" / "references" / "tool-patterns.md",
)


def test_active_docs_do_not_restore_removed_protocol_surface() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in ACTIVE_PROTOCOL_DOCS)

    assert "computer_double_click" not in text
    assert "data.screenshots" not in text
    assert "top-level `state_text`" not in text
    assert "顶层别名 `state_text`" not in text


def test_repository_mcp_config_uses_stable_cli_entrypoint() -> None:
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))

    assert config["mcpServers"] == {
        "cua-win": {
            "command": "uv",
            "args": ["run", "cua-win", "mcp"],
            "cwd": ".",
            "env": {"PYTHONIOENCODING": "utf-8"},
        }
    }


def test_manual_release_default_matches_package_version() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^\s+default:\s+"([^"]+)"\s*$', workflow, re.MULTILINE)

    assert match is not None
    assert match.group(1) == __version__
