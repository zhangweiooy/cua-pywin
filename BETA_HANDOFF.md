# cua-win 0.2.6 Beta Handoff (historical)

Status: archived snapshot of the 0.2.6 beta. Its versions, artifact paths,
Florence pipeline and validation counts describe that earlier build and are
not current acceptance evidence. For the 0.3.4 working tree with Unreleased
Florence-removal changes, read [README](README.md),
[Release Guide](RELEASE.md), and
[visual runtime verification](docs/yolo-onnx-poc.md).

This document preserves the beta handoff and its original workflows for
historical reference.

## State at the 0.2.6 handoff

- Repository: `D:\Documents\Code\cua-pywin`
- Branch: `master`
- Package/server version: `0.2.6`
- Public protocol: `0.2`
- Public MCP surface: 25 tools
- Repository visibility: private
- 0.2 protocol reset commit: `fb2694c Implement cua-win 0.2 protocol reset`

Expected release artifact locations:

- Installer: `dist-installer\cua-win-0.2.6-win64-setup.exe`
- Wheel: `dist\cua_pywin-0.2.6-py3-none-any.whl`
- Standalone: `dist-nuitka\cua-win.dist\cua-win.exe`

Build artifacts are ignored and can be newer or older than this handoff. Treat
the generated `dist\SHA256SUMS.txt`,
`dist-nuitka\cua-win.dist\SHA256SUMS.txt`, and
`dist-installer\SHA256SUMS.txt` as authoritative for the files beside them.

Local validation recorded for that handoff:

- Full pytest, Ruff, compileall, skill validation, and release validation pass.
- Standalone `list-tools` reports 25 tools and full `doctor` passes 31/31
  checks.
- Default standalone daemon start/call/stop succeeds with protocol `0.2`.
- The pruned standalone is about 1,947 MiB and contains 14,620 files. Runtime
  execution does not generate `__pycache__/*.pyc`.
- Hybrid builds use a locked production-only staging environment. The runtime
  omits duplicate RapidOCR default model files, OpenCV FFmpeg, Polars,
  and third-party tests/examples/docs. `pydoc`/`pydoc_data` are retained
  because the normal Florence-2 path loads them through SciPy.
- A forced standalone SOM observation must complete YOLO, bundled RapidOCR,
  and Florence-2 inference with OCR text and icon captions returned. The
  default OCR timeout is 20 seconds to accommodate cold CPU initialization.
- UIA patterns are queried into concrete COM interfaces, text-node actions can
  resolve to a nearby actionable ancestor, and unavailable patterns fall back
  without exposing `IUnknown` attribute errors.
- Standalone builds generate and freeze comtypes UIAutomationCore,
  Accessibility, and stdole bindings plus `comtypes.stream`, remove
  build-machine typelib mtimes, and retain the code-generator version check.
  Every build starts, observes, and stops Teaching against the produced
  executable with private state and APPDATA directories, then verifies process,
  temporary-directory, and comtypes-cache cleanup.
- Installer builds perform a real silent install into a private directory,
  validate full doctor, 25 tools, repeated screenshots, UIA/WGC observation,
  two Teaching lifecycles, and the complete SOM path, then uninstall and reject
  PATH, process, registry, installation-directory, or state residue.
- Read-only Notepad, Chrome, Windows Terminal, and WeChat observations complete
  with WGC capture; WeChat remains semantically degraded and is the main
  application-specific tuning target.

## Protocol Reset

Protocol 0.2 intentionally has no 0.1 compatibility mode.

Every tool returns:

```json
{
  "protocol_version": "0.2",
  "success": true,
  "data": {},
  "artifacts": [],
  "warnings": [],
  "error": null,
  "action": null
}
```

Key rules:

- Read tool-specific fields from `data`, never from MCP text content.
- Target inputs use only `window: {id, pid}`. Both fields are required.
- Public `window_id`, `windowId`, top-level target `pid/window_id`, and
  `snapshot_id` are removed.
- `element_index` requires the latest `observation_id`.
- Window screenshot coordinates and `computer_zoom` require the latest
  `screenshot_id`.
- Zoom coordinates require the current `zoom_id`.
- A new observation immediately stales old observation, screenshot, and zoom
  bindings for the same window.
- Images are MCP image content. `artifacts[].content_index` identifies each
  image; `data` contains no base64 image or debug output path.
- Side-effect calls return a transient action outcome. For
  `outcome="unknown"`, MCP `isError`, or stale state, re-observe before any
  retry.
- Recording start requires a new or empty output directory. Replay validates
  both the manifest and every turn as protocol `0.2`.

Daemon `/call` uses the fixed
`{protocol_version,result,content,transport_error}` wrapper. CLI normal and
`--json` output print the result envelope. Use `--output PATH` for the primary
image and `--output-dir DIR` for all image artifacts.

## Historical Fresh Session Template

```text
Project: D:\Documents\Code\cua-pywin
Current version: 0.2.6 beta
Current branch: master
Goal: continue beta testing, real-app tuning, and small verified iterations.
First read AGENTS.md, BETA_HANDOFF.md, README.md, and
skills/cua-win/SKILL.md. Read tool-patterns.md only for exact tool/CLI forms.
Do not restore 0.1 protocol aliases or add an action ledger.
Keep the public MCP surface at 25 tools.
```

Then check:

```powershell
git status --short
git log -3 --oneline
```

## Development Checks

```powershell
uv run pytest -q
uv run ruff check .
python -m compileall -q computer_server som
uv run python scripts/mcp_test_one.py --list
uv run cua-win computer_learn --help
uv run cua-win computer_screenshot --help
uv run cua-win computer_manage_window --help
'{"action":"status"}' | uv run python scripts/mcp_test_one.py computer_learn - --timeout 120
uv run --no-sync python "C:\Users\vinfl\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills/cua-win
uv run python scripts/validate_release.py --version 0.2.6
```

Expected:

- Full pytest suite passes.
- Ruff and compileall pass.
- Tool list reports exactly 25 tools.
- `computer_learn --help` reports the session and incremental timeline
  arguments without starting the daemon.
- `computer_screenshot --help` reports optional canonical `window` capture
  without adding another public tool.
- `computer_manage_window --help` states that Windows `action=activate`
  restores minimized targets, verifies foreground activation, and does not
  inject synthetic key input.
- MCP initialize reports server version `0.2.6`.
- `computer_learn status` returns a valid protocol 0.2 envelope.
- Skill and release validation pass.

## Install Smoke

On a clean Windows machine when possible:

```powershell
Get-FileHash .\cua-win-0.2.6-win64-setup.exe -Algorithm SHA256
.\cua-win-0.2.6-win64-setup.exe
where.exe cua-win
cua-win doctor
cua-win list-tools
cua-win start
cua-win computer_get_screen_size
cua-win stop
```

Expected:

- The checksum matches the adjacent generated `SHA256SUMS.txt`.
- `doctor` has no failures.
- `list-tools` reports 25 tools.
- Calls return the fixed 0.2 result envelope.
- Daemon start/status/stop and stale-state recovery remain healthy.

## Real App Smoke

Prioritize Notepad, Chrome/Edge, Windows Terminal, WeChat, and Chinese UI.

Use a fresh canonical window:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
{"query":"记事本 Notepad"}
'@ | cua-win call computer_list_windows -
```

Observe:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "response_mode": "compact"
}
```

Compact observations cap the image at 960 pixels and returned nodes at 30 while
omitting verbose diagnostics. The full tree is disabled by default; add
`"include_tree_markdown": true` only when tree diagnostics are required.
Public floating-point values are rounded to two decimal places without
reducing internal coordinate precision.
Public data keeps `state_text` as the single summary text, removes exact
duplicate node counts, and exposes capture diagnostics at
`capture.capture_metadata`. Empty diagnostic fields are omitted.

Then verify:

1. An indexed action with the latest `observation_id` succeeds.
2. A screenshot-coordinate action with the latest `screenshot_id` succeeds.
3. A second observation makes the first IDs return `STALE_OBSERVATION`.
4. A zoom made from the latest screenshot works; a newer observation stales it.
5. Chinese input is written to the intended foreground target.
6. Screenshot images are content blocks with matching artifacts.
7. `capture_after` and required verification share one fresh observation.

For the read-only app matrix:

```powershell
uv run python scripts/windows_robustness_smoke.py
```

## Teaching Smoke

The UIA learning runtime owns a dedicated STA worker and must work from an MTA
caller:

```powershell
$sessionId = "cua-teaching-smoke"
@"
{"action":"start","session_id":"$sessionId","task_description":"0.2 teaching smoke"}
"@ | cua-win call computer_learn -

cua-win call computer_learn '{action:status}'
@"
{"action":"stop","session_id":"$sessionId"}
"@ | cua-win call computer_learn -
```

Confirm:

- start returns `data.status="recording"`;
- status returns `data.recording=true`;
- a real click increases action/event data;
- stop returns strict actions plus the bounded duration/action/observation/
  degraded-observation summary;
- concurrent or repeated stop/finalize is idempotent;
- start failure leaves no active hook, worker, or half-started session.

## Build

```powershell
./scripts/build_release.ps1 -Version 0.2.6
./scripts/build_nuitka.ps1 -Version 0.2.6
./scripts/build_installer.ps1 -Version 0.2.6
```

Standalone smoke:

```powershell
.\dist-nuitka\cua-win.dist\cua-win.exe list-tools --no-daemon --json
.\dist-nuitka\cua-win.dist\cua-win.exe doctor --no-desktop --json
```

## Iteration Rules

- Keep 25 public tools unless real agent evidence justifies a surface change.
- Do not restore 0.1 aliases or mixed result shapes.
- Keep action outcomes transient; do not add ledger/query/idempotency systems.
- Treat `ACTION_OUTCOME_UNKNOWN` as a re-observation requirement.
- Keep internal HWND/window references private to services and replay.
- Keep sensitive learning contexts redacted or skipped.
- Keep model updates bundled and developer-controlled; no runtime downloads.
- Keep localhost, daemon token, process ownership, and stale-state protections.

## References

- `AGENTS.md`: architecture, constraints, validation, and current priorities
- `README.md`: user-facing setup and protocol overview
- `RELEASE.md`: packaging and release workflow
- `skills/cua-win/SKILL.md`: agent workflow
- `skills/cua-win/references/tool-patterns.md`: exact calls and CLI patterns
