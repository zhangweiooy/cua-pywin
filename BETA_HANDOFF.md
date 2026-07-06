# cua-win 0.1.3 Beta Handoff

This document is the short handoff for a fresh session, beta tester, or future
feature-iteration pass. It intentionally avoids project history and focuses on
the current working state.

## Current State

- Repository: `D:\Documents\Code\cua-pywin`
- Branch: `master`
- Version: `0.1.3`
- Latest packaged source commit: `bfeb43a Prepare 0.1.3 beta notes`
- Public MCP surface: 25 tools
- Repository visibility: private

Built artifacts:

- Installer: `dist-installer\cua-win-0.1.3-win64-setup.exe`
- Installer SHA256:
  `555ef7caa1cd56576777eb3867774b8b338a2959880badfada97d13deb557bb9`
- Wheel: `dist\cua_pywin-0.1.3-py3-none-any.whl`
- Wheel SHA256:
  `68869edd6de157ec779d293e0b196e997dd1ceaa841d8381209e4213672c0398`
- Standalone directory: `dist-nuitka\cua-win.dist`
- Standalone exe SHA256:
  `1518fce17e6af524f7b8f6e39b32d2bf0482e0cac9ffa1cc00d4e93917cbca71`

## Start A Fresh Session

Use this prompt when continuing in a new Codex session:

```text
Project: D:\Documents\Code\cua-pywin
Current version: 0.1.3 beta
Current branch: master
Goal: continue beta testing and feature iteration from the current codebase.
Do not re-open old architectural debates unless new evidence appears.
First read AGENTS.md, README.md, and BETA_HANDOFF.md.
Prioritize concrete beta feedback, failing commands, real app smoke tests, and
small verified improvements.
```

Start by checking the repository state:

```powershell
git status --short
git log -3 --oneline
```

## Install Smoke

Use a clean Windows machine when possible. The development machine has already
run local release smoke, but it is not a substitute for clean-machine install
validation.

1. Run `dist-installer\cua-win-0.1.3-win64-setup.exe`.
2. Enable `Add cua-win to the current user's PATH`.
3. Open a new PowerShell window.
4. Run:

```powershell
where.exe cua-win
cua-win doctor
cua-win list-tools
cua-win mcp-config
cua-win mcp-config --client claude
cua-win start
cua-win computer_get_screen_size
cua-win stop
```

Expected:

- `where.exe cua-win` resolves to the installed directory.
- `doctor` has no failures.
- `list-tools` reports 25 tools.
- `mcp-config` prints a `.mcp.json` shape using `{"command":"cua-win","args":["mcp"]}`.
- `mcp-config --client claude` prints a PowerShell-safe `claude mcp add-json`
  command.
- `computer_get_screen_size` returns screen dimensions.

## App Smoke

Run at least one smoke pass on each of:

- Notepad
- Windows Terminal
- WeChat
- DingTalk
- Chrome / Edge

Suggested flow:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
{"query":"微信"}
'@ | cua-win call computer_list_windows -

@'
{"app":"微信","wait_for_window":true,"wait_timeout":10}
'@ | cua-win call computer_launch_app -
```

Then use the returned canonical `window` with:

```powershell
cua-win call computer_get_window_state @args.json
```

`args.json` example:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "max_nodes": 100
}
```

For Chinese JSON through PowerShell stdin, set UTF-8 output first:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
```

Using `@args.json` files is usually safer for nested JSON or Chinese text.

For a compact read-only diagnostics pass across common apps, run:

```powershell
uv run python scripts/windows_robustness_smoke.py
```

The script prints JSON summaries for target profile, capture reliability, tree
quality, and provider-error keys. It does not print screenshots or node text.

## Development Checks

Before committing code changes:

```powershell
uv run pytest -q
uv run ruff check .
python -m compileall -q computer_server som
uv run python scripts/mcp_test_one.py --list
'{"action":"status"}' | uv run python scripts/mcp_test_one.py computer_learn - --timeout 120
uv run --no-sync python "C:\Users\vinfl\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills/cua-win
uv run python scripts/validate_release.py --version 0.1.3
```

Expected current baseline:

- `pytest`: 266 passed
- `ruff`: all checks passed
- tool list: 25 tools
- MCP `computer_learn` stdin smoke: pass
- skill validation: valid

## Build Commands

Fast local standalone rebuild after dependencies are already synced:

```powershell
./scripts/build_nuitka.ps1 -Version 0.1.3 -SkipSync -SkipChecks
./scripts/build_installer.ps1 -Version 0.1.3
```

Full wheel/release candidate validation:

```powershell
./scripts/build_release.ps1 -Version 0.1.3
```

Standalone smoke:

```powershell
.\dist-nuitka\cua-win.dist\cua-win.exe list-tools --no-daemon --json
.\dist-nuitka\cua-win.dist\cua-win.exe doctor --no-desktop --json
```

Note: `cua-win.exe --version` is not currently a supported CLI command.

## Feature Iteration Rules

- Keep the public MCP surface at 25 tools unless real agent usage shows a
  concrete need to add or split a tool.
- Prefer canonical `window` objects over hand-built `pid/window_id`.
- Prefer `screenshot_id` for screenshot-coordinate actions.
- Keep `computer_get_window_state` usable as the primary observation tool; add
  complexity only when real latency, payload, or accuracy evidence justifies it.
- Treat `action_path.target_profile`, `delivery_risk`, `capture_reliability`,
  `semantic_degraded_reason`, and `provider_errors` as diagnostic metadata.
  They should explain Windows risk without changing default tool behavior.
- Keep `computer_learn` timeline additions additive. Existing `actions[]`
  consumers must continue to work when `observations[]` and quality metrics are
  ignored.
- Do not expose broad background dispatch or PostMessage input without
  verification gates and app compatibility evidence.
- Treat localized app handling as a normal product behavior, not a user-visible
  workaround.
- Do not expose internal fallback mechanics as user-facing instructions. The
  user-facing contract should be stable behavior, canonical `window`, and
  screenshot-bound coordinates.
- Keep model updates developer-controlled and bundled in releases; do not add
  runtime model downloads for end users.

## Feedback Template

```text
Version:
Artifact tested:
OS / Windows build:
Install result:
PATH result:
doctor result:
App tested:
Command:
Expected:
Actual:
Logs / screenshots:
Reproducible:
Severity:
Notes:
```

## Useful References

- `AGENTS.md`: repository conventions, architecture, priorities, release rules
- `README.md`: user-facing setup and tool overview
- `RELEASE.md`: release flow and packaging notes
- `skills/cua-win/SKILL.md`: Codex skill instructions
- `skills/cua-win/references/tool-patterns.md`: exact tool and CLI patterns
