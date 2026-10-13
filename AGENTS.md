# AGENTS.md

This file provides guidance to coding agents working in this repository.

# cua-pywin

Windows UI automation MCP server — 25 tools for computer control.

**Priority: Windows + Chinese UI is the highest-guarantee target.**

## Architecture

```
computer_server/
  mcp_server.py             — 25 MCP tools via FastMCP; tool wrappers,
                              recording/replay/learning entrypoints
  main.py                   — Shared HTTP app: /status, /mcp, and
                              token-protected cua-win JSON RPC routes
  tool_cli.py               — cua-win CLI, HTTP server launcher, background
                              daemon lifecycle, metadata, and HTTP client
  tool_rpc.py               — Shared tool schema/result JSON serialization
  server_context.py         — Per-instance mutable state: recording session,
                              learning recorder, observation registry,
                              UIA/MSAA/action gates, popover dedup, held input state
  handlers/                 — Platform primitives and OS-facing wrappers
    base.py                 — Abstract handler interfaces
    factory.py              — HandlerFactory + Handlers dataclass
    windows.py              — Windows automation: ImageGrab, win32gui, pynput,
                              clipboard lock, command execution
    windows_uia.py          — UIA COM tree walker
    windows_msaa.py         — MSAA / IAccessible fallback tree walker for
                              classic Win32/VCL/SAL-style controls
    windows_wgc.py          — Windows.Graphics.Capture target-window capture
                              backend with D3D11 texture readback
    windows_window.py       — Windows foreground-lock bypass and activation
    generic.py              — Cross-platform desktop and window helpers
    element_cache.py        — Per-(pid, window_id) element cache with TTL,
                              HWND validity, optional window origin
    zoom_registry.py        — Per-(pid, window_id) zoom context plus zoom_id
    som_bridge.py           — SOM OmniParser bridge and warm-up state
    browser_cdp.py          — Chrome DevTools Protocol client with target_id,
                              target cache, cache-miss refresh
    control_types.py        — UIA/AX semantic role normalization
    screen_geometry.py      — Virtual screen geometry and DPI-aware metrics
    screenshot_service.py   — Image resize / encode helpers
  services/                 — Application services consumed by MCP tools
    protocol_service.py     — Protocol 0.2 envelope, errors, artifacts, and
                              transient action outcomes
    observation_context.py  — Per-window observation/screenshot/zoom freshness
    action_delta_service.py — Optional before/after action summaries
    agent_state_service.py  — Compact observation text for agent reasoning
    coordinate_service.py   — Coordinate conversion and zoom resolution
    window_state_service.py — WindowSnapshotGeometry, UIA collection,
                              SOM merge, window state snapshots, zoom images
    window_identity_service.py — public window object normalization,
                              window_ref creation, and stale HWND resolution
    replay_resolver.py      — Replay argument resolution for moved windows and
                              element relocation
    capture_service.py      — Virtual desktop capture shared by recording and
                              learning
    ocr_service.py          — Lazy OCRProcessor singleton
    target_profile_service.py — Window family classification and delivery risk
    permission_service.py   — Runtime permission policy for open,
                              commands, CDP JavaScript
    uia_service.py          — UIACallGate for sync UIA calls in hook threads
    validation_service.py   — Shared enum/range/path/tool parameter validation
  recording/                — Agent-perspective recording / replay
    session.py              — Turn-folder lifecycle, bounded writer queue,
                              drop/error counters
    replay.py               — Trajectory replay engine
  learning/                 — Human-perspective recording
    session.py              — ObservedAction, ElementContext, LearningSession
    coalescer.py            — Keystroke and scroll coalescers
    recorder.py             — pynput hooks, UIA enrichment, OCR, suppression
                              of agent-injected input, sensitive-context gate
    platform.py             — Platform adapters for app/UIA/screenshot/vision
  overlay/                  — Agent cursor overlay
    cursor.py               — Overlay window, GDI resources, animation loop
    motion.py               — Dubins path planner, Phase enum, Pose dataclass
```

### Dependency Direction

```
mcp_server.py -> services/* -> handlers/* -> OS APIs
                      ^
                      +-- recording/, learning/, overlay/
```

`services/` owns cross-cutting orchestration such as UIA gating, geometry,
replay resolution, capture, OCR, and permissions. `handlers/` should remain
thin OS-facing primitives and should not import from `services/`.

## Setup

```bash
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple
python -c "import computer_server; print('OK')"  # should complete in <10s
```

## Key Commands

```bash
# Start MCP server in stdio mode
cua-win mcp

# Print copy-paste-safe MCP client config / install commands
cua-win mcp-config
cua-win mcp-config --client claude
cua-win mcp-config --client codex

# Start foreground HTTP server
cua-win serve --port 8000

# Start foreground HTTP server without cua-win JSON RPC routes
cua-win serve --port 8000 --no-cli-rpc

# Background daemon/client flow
cua-win doctor
cua-win start
cua-win status
cua-win list-tools
cua-win help computer_learn
cua-win computer_learn --help
cua-win computer_get_screen_size
@'
{"window":{"id":5678,"pid":1234},"format":"png","max_image_dimension":1280}
'@ | cua-win call computer_screenshot - --output window.png
@'
{"window":{"id":5678,"pid":1234}}
'@ | cua-win call computer_get_window_state -
cua-win computer_press_key '{\"key\":\"Control_L+s\"}'
cua-win computer_mouse '{\"action\":\"move\",\"x\":100,\"y\":100}'
cua-win computer_recording '{action:status}'
cua-win computer_learn '{action:status}'
'[{"type":"wait","milliseconds":100},{"tool":"computer_get_screen_size","arguments":{}}]' | cua-win batch
cua-win stop

# Single-tool tests
uv run python scripts/mcp_test_one.py <tool_name> [json_args|-|@file] --timeout 120
uv run python scripts/mcp_test_one.py --list

# Compile checks
python -m compileall -q computer_server som

# Agent smoke scaffolding
uv run python scripts/agent_smoke_fixture.py
uv run python scripts/run_agent_smoke.py --dry-run --scenario fixture-basic

# Validate bundled Codex skill
uv run --no-sync python "C:\Users\vinfl\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills/cua-win

# Build and validate a release candidate
./scripts/build_release.ps1 -Version 0.4.0

# Build a local Nuitka standalone executable directory
./scripts/build_nuitka.ps1
./scripts/build_nuitka.ps1 -Version 0.4.0

# Build a Windows setup.exe from the standalone output
./scripts/build_installer.ps1 -Version 0.4.0

# Fast local Nuitka iteration after dependencies are already synced
./scripts/build_nuitka.ps1 -SkipSync -SkipChecks
```

## Coordinate Systems

| Space | Description |
|---|---|
| Screen coords | Absolute virtual-desktop position. `get_window_bounds` returns this. |
| Full-screen pixels | `automation_handler.screenshot()` output, including virtual desktop origin. |
| Window pixels (full-res) | Window-local pixels before `max_image_dimension` downscale. |
| Window pixels (resized) | Coordinates in the screenshot returned by `computer_get_window_state`. |
| Zoom pixels | Coordinates in the image returned by `computer_zoom`. |

`computer_click`, `computer_drag`, and `computer_scroll` accept optional
`coordinate_space`: `screen`, `window_resized`, `window_full_res`, `zoom`, or
`element`. If omitted, `element_index` means `element`, `zoom_id` means `zoom`,
and all other coordinates mean `screen`. Window-relative coordinate spaces
must be selected explicitly.

`computer_get_window_state` returns both `observation_id` and `screenshot_id`.
`observation_id` binds the observation/node snapshot and rejects stale element
actions. `screenshot_id` binds the exact screenshot/crop geometry used for
`window_resized` and `window_full_res` coordinate conversion; prefer passing it
to `computer_click`, `computer_drag`, `computer_scroll`, and `computer_zoom`
when coordinates come from a returned screenshot. `computer_zoom` returns
`zoom_id`; zoom-coordinate actions must use it. A new observation immediately
stales older observation, screenshot, and zoom bindings for the same window.
Returned `nodes` use `nodes_coordinate_space="window_resized"`, so their
bounding boxes and centers align with the returned target screenshot. By
default, only the 100 highest-priority nodes are returned; set `max_nodes=0` to
return every node or use `query`/`max_nodes` to narrow or expand the payload.
This return limit does not affect the internal `ElementCache`, which continues
to store complete window-full-res coordinates. Nodes include low-level
`actions` plus agent-facing `action_hints` such as `click`, `press`, `focus`,
`type`, `set_value`, `select`, `toggle`, `expand`, and `scroll`.
`data.accessibility_hint` and `data.observation_recommendation` summarize
whether UIA/MSAA semantics are sufficient, whether vision/OCR was needed, and
whether callers should narrow with `query` or raise `max_nodes`.
`data.state_text` provides the default agent-facing observation summary, while
`data.agent_state` contains its truncation and sampling metadata without a
duplicate text copy. The text is capped by `agent_text_limit=500` by default;
use `show_full_text=true` or raise `agent_text_limit` only when the compact
state is insufficient. `tree_markdown` is omitted by default and is
returned only with `include_tree_markdown=true`. Use
`response_mode="compact"` for a bounded 960-pixel, 30-node response without
verbose capture/provider/vision diagnostics. Public window-state floats are
rounded to two decimal places; internal freshness and coordinate geometry keep
their original precision.
Public data keeps canonical node counts; exact duplicate counts and empty
diagnostic fields are omitted.

Key invariant: `ElementCache` stores window-relative coordinates, and
`_resolve_element_center` returns screen coordinates.

## Protocol 0.2 Contract

All 25 public tools return the fixed
`protocol_version/success/data/artifacts/warnings/error/action` envelope.
Expected validation, target, observation, permission, provider, capture, action,
and postcondition failures are normal MCP results with `success=false`.
Uncaught exceptions and transport failures use MCP `isError`.

Tool-specific fields belong in `data`. Images exist only as MCP image content
and are described by `artifacts[]`; public structured payloads must not contain
base64 image data or debug output paths. Side-effect calls return a transient
`action_id` with `completed/rejected/unknown` and retry safety. Do not add an
action ledger, persistence, idempotency store, or query tool without new
evidence.

## Window Identity Contract

Targeted public tools accept only canonical `window`:

```json
{"window":{"id":5678,"pid":1234}}
```

The only public window fields are `id` and `pid`, and both are required.
`window_id`, `windowId`, extra window metadata, and top-level target
`pid/window_id` are rejected. `computer_list_windows`, `computer_list_apps`,
`computer_get_window`, and `computer_get_window_state` return canonical
`window` objects for follow-up calls. Use
`computer_get_window({"id":..., "app":...})` to rehydrate a stale or lost
window binding, and `computer_get_window({"active":true})` for the current
foreground window.

Merged public entry points:

- Hotkeys use `computer_press_key({"keys":[...]})` or
  `computer_press_key({"key":"Control_L+s"})`.
- `computer_type` and `computer_press_key` accept canonical `window`; targeted
  text/key input activates and verifies the target first. Calls without a
  window preserve current-focus behavior, while key-up skips activation so a
  held key can always be released.
- `computer_click`, `computer_drag`, and `computer_scroll` activate and verify
  an explicitly supplied canonical `window` before mouse input.
- Keyboard down/up primitives use
  `computer_press_key({"key":"Shift","action":"down|up"})`.
- Mouse move/down/up primitives use `computer_mouse({"action":"move|down|up"})`.
- Window activate/minimize/maximize/close/move/resize use
  `computer_manage_window`.
- Recording start/stop/status uses `computer_recording`.
- Learning start/stop/status uses `computer_learn`.
- Learning start and stop require the same non-empty `session_id`; status
  supports bounded incremental reads with `after_sequence` and
  `timeline_limit`, and callers continue from `data.next_sequence`.
- App/title/query window search uses `computer_list_windows`.
- Accessibility-tree reads use `computer_get_window_state` `nodes`; request
  `tree_markdown` explicitly with `include_tree_markdown=true` for diagnostics.

`window_ref` is separate replay metadata. Agents should not hand-author it;
recording builds it from the resolved `window` plus origin, process, title,
class, and size metadata.

`computer_launch_app(wait_for_window=True)` polls for a visible, targetable
top-level window and returns its canonical `window`. Matching prefers the
launcher PID, then resolved process name, then app/title hints, because apps
such as WeChat can reuse an existing process or expose the main HWND from a
different PID. A timeout preserves the successful launch result but reports
`window_ready=false` explicitly.

## Key Design Decisions

### Per-window Screenshot Capture

- `PrintWindow` fails on hardware-accelerated windows such as Chrome and GPU apps.
- `get_window_state` first tries Windows.Graphics.Capture for target-window
  pixels, which avoids overlapping foreground-window pollution.
- The fallback path is full virtual-desktop capture plus `GetWindowRect` crop.
- Window, crop, screenshot origin, and resize ratio are tracked explicitly for
  negative-monitor and partially offscreen windows.

### UIA Integration

- `window_state_service.collect_uia_nodes` calls `handlers.windows_uia.walk_uia_tree`.
- `UIAWalkGate` prevents stacked UIA walks against a hung provider; timed-out
  providers are marked unhealthy briefly and can fall through to SOM.
- Accessibility tree output is exposed through `computer_get_window_state`,
  which uses the protected UIA walk path instead of calling `walk_uia_tree`
  synchronously in a standalone tool handler.
- UIA action calls (`InvokePattern.Invoke` and `ValuePattern.SetValue`) run
  behind `UIAActionGate`; timeout or in-flight conflicts fall back to pixel/HID
  paths rather than blocking the action tool indefinitely.
- `CachedElement.platform_ref` is deprecated. Invoke and ValuePattern actions
  resolve a fresh UIA element by point at action time.

### Tree Quality, MSAA, And SOM

`get_window_state` reports UIA, MSAA, and vision quality separately. A bounded
MSAA observation pass runs behind `MSAAWalkGate` alongside UIA because it is
cheap and can preserve classic Win32 roles that UIA proxies collapse. Timed-out MSAA
providers are marked unhealthy by window so repeat requests do not stack leaked
COM calls. MSAA only promotes the combined semantic tree quality when it
contributes usable indexed nodes. SOM vision is used when the combined semantic
tree quality is still poor or empty.

MSAA nodes carry source-specific metadata such as `msaa_role`,
`msaa_role_id`, `msaa_role_hex`, `msaa_default_action`, `msaa_state`,
`msaa_child_id`, and `msaa_has_location`. Dropdown-style MSAA roles
(`BUTTONDROPDOWN`, `BUTTONMENU`, `BUTTONDROPDOWNGRID`, `SPLITBUTTON`) expose
`actions=["invoke","expand"]`; `computer_perform_secondary_action` with
`action="expand"` clicks the right edge of the cached MSAA rect to target the
dropdown half. Primary actions remain on `computer_click`; window activation
remains on `computer_manage_window`.

### Recording Vs Learning

- Recording captures MCP tool invocations into `turn-NNNNN/` folders and replays
  via tool re-invocation.
- Learning captures human actions through pynput, UIA, OCR, and screenshots; it
  is used for demonstration analysis, not direct tool replay.
- Agent-injected input is suppressed during learning so automation actions are
  not recorded as human demonstration.

### Replay Resolution

Recording stores `window_ref` and, for element actions, `element_ref`. Replay
tries to resolve current windows by live HWND, process name, then title, and can
relocate elements using cached semantic metadata before falling back to recorded
coordinates. Public callers should pass returned `window` objects; `window_ref`
is internal replay metadata.

### HTTP / Network Boundary

- `cua-win serve` defaults to `127.0.0.1:8000`.
- `cua-win start` launches a background localhost daemon on a random port.
- Non-localhost binding requires `--i-know-this-exposes-my-computer`.
- The shared HTTP app always exposes `/status` and `/mcp`.
- When `cua-win` daemon RPC is enabled, `/tools`, `/tools/{name}`, `/call`, and
  `/shutdown` are enabled behind the per-daemon `X-Cua-Token` header.
- `cua-win serve --no-cli-rpc` disables the JSON RPC routes and metadata.
- The HTTP surface is `/status`, `/mcp`, and token-protected cua-win JSON RPC routes.
- CORS allows only localhost origins.
- High-privilege control flows through MCP tools.
- `python -m computer_server` is not a supported compatibility entrypoint;
  use `cua-win serve`.
- `cua-win doctor` is a read-only local diagnostic command. In addition to
  dependency, model, state-dir, and daemon probes, it checks the current
  Windows session/desktop, EnumWindows visibility, and full-screen screenshot
  availability without starting the daemon or sending input. Use
  `--no-desktop` for CI or headless environments.

### Runtime Permissions

- `CUA_DISABLE_CDP_JAVASCRIPT=1` disables CDP JavaScript execution.

## Feature Notes

- `force_frontmost=True` activates a target window and uses `win32api.mouse_event`
  for HID-level clicks needed by some WebView2/UWP targets.
- Windows activation restores minimized targets, temporarily attaches the
  relevant input threads, verifies the target became foreground, and never
  injects a synthetic Alt keystroke.
- `computer_click` returns action-path metadata (`method`, foreground/system
  input flags, visibility requirements, coordinate source) so callers can tell
  whether the action used UIA Invoke, force-frontmost mouse events, or normal
  pixel click.
- Double-click uses `computer_click(count=2)`; there is no separate public
  double-click tool.
- `computer_get_window_state` returns target and related transient images as
  MCP content bound through envelope `artifacts[]`. Each artifact identifies
  its `role`, canonical `window`, `observation_id`, `screenshot_id`, dimensions,
  byte count, and `content_index`; structured `data` has no screenshots array.
- `computer_screenshot` accepts an optional canonical `window` for lightweight
  target-window pixels. It reuses the WGC-first/screen-crop-fallback path
  without UIA/MSAA/SOM and does not create observation/screenshot freshness
  bindings; use `computer_get_window_state` when follow-up actions need them.
- `computer_perform_secondary_action` is the narrow entry point for
  non-primary node actions. It currently supports only the observable MSAA
  dropdown/splitbutton `expand` path and activates the target before input.
- `computer_click(verify_query=..., require_verification=True)` performs a
  post-action window-state check and marks the result failed when the query is
  not observed. A canonical `window` is required for verification.
- `capture_after=True` is supported by click, scroll, type, and secondary-action
  tools. `data.capture_after` is a fresh observation with `observation_id`,
  `screenshot_id`, capture metadata, and nodes. Pass
  `include_post_action_delta=True` only when a lightweight before/after
  comparison is worth an extra pre-action observation; then the result also
  includes `data.post_action_delta` summarizing node count, added/removed nodes,
  text changes, tree-quality changes, and before/after screenshot ids.
- Window snapshots include `capture.capture_metadata` with screenshot provenance,
  crop geometry, resize metadata, capture backend, fallback reason, and
  explicit occlusion-risk warnings. `capture.capture_metadata.stage_timings` includes
  bounds lookup, WGC attempt, fallback activation, screen capture, decode,
  crop/resize, and total capture latency. WGC backend timings additionally
  split item creation, D3D setup, session setup, frame wait, and texture
  readback. WGC snapshots report
  `capture_backend="wgc"` and `occlusion_safe=True`; fallback screen crops
  report `capture_backend="screen_crop"` and `occlusion_risk=True`.
- `computer_zoom` follows the last snapshot backend: WGC snapshots use WGC for
  native zoom crops and fall back to screen crop only when WGC fails.
- Disabled UIA elements return an explicit error; callers should raise/focus the
  window and re-snapshot.
- `computer_release_all_inputs` releases tracked held keys and mouse buttons
  after interrupted `computer_press_key(action="down")`,
  `computer_mouse(action="down")`, or replay failures.
- Clipboard typing uses a process-level lock and restores clipboard on type
  failures when possible.
- `computer_page` requires an explicit CDP port or browser
  `--remote-debugging-port`; it does not silently fallback to 9222. JavaScript
  execution requires `allow_javascript=True`; `innerHTML` extraction requires
  `allow_html=True`.

## SOM Vision Strategy

### Current Runtime: Full OmniParser V2 + PaddleOCR ONNX

```
OCR: RapidOCR + ONNXRuntime default, PaddleOCR PP-OCRv4 mobile Chinese+English
Icon detection: bundled OmniParser V2 YOLO
UI understanding: main LLM reads the full screenshot with UIA/MSAA/OCR/YOLO nodes
Runtime: ONNX Runtime CPU; benchmark cold and warm latency separately
Risk: OmniParser is trained on modern Web/mobile UI; old Win32 recall may vary
```

The OmniParser V2 YOLO detector is bundled as
`som/weights/icon_detect/model.onnx` from the current specialized
`microsoft/OmniParser-v2.0` weight. Runtime preprocessing, decoding, and NMS use
NumPy/OpenCV with ONNX Runtime CPU. PyTorch, Torchvision, and Ultralytics are
allowed only in the export/benchmark dependency group and must not appear in
the wheel, standalone, installer, or production process.
The detector uses four intra-op threads, one inter-op thread, disables the CPU
memory arena, and keeps memory-pattern reuse enabled.

RapidOCR det/cls/rec ONNX models and the Chinese character dictionary are
bundled under `computer_server/models/rapidocr_ppocrv4/`. Runtime initialization
passes these paths explicitly, so OCR does not depend on RapidOCR's package
layout, default model selection, cache, or network download. The ONNX files are
tracked with Git LFS; `cua-win doctor` verifies their SHA-256 hashes.
RapidOCR fixes ONNX Runtime to four intra-op threads and one inter-op thread;
its CPU arena remains disabled.

The main LLM interprets the full-window screenshot using UIA/MSAA semantics,
OCR text, and YOLO icon locations. Keep YOLO nodes unnamed and actionable with
their element indexes, bounding boxes, centers, and detection confidence.
Prefer an existing `element_index` plus `observation_id` over estimating small
control coordinates. Use `computer_zoom` with `screenshot_id` only when a small
region needs closer inspection. Do not add icon-caption models or per-icon LLM
API calls. Preserve ambiguous OCR/icon pairs for the caller to interpret.

Relevant controls:

- `CUA_OCR_TIMEOUT_SECONDS` overrides per-call OCR timeout.
- `CUA_RAPIDOCR_DEVICE=cpu` is the supported RapidOCR runtime in v1.
- `CUA_RAPIDOCR_MODEL_DIR` is a developer-only local bundle override. It must
  never trigger downloads or become an end-user model update mechanism.
- `CUA_OCR_DENOISE=0` disables OCR text cleanup; `CUA_OCR_DENOISE=strict`
  enables deletion-style filtering for isolated symbol/single-character
  artifacts. Default denoising must stay conservative.

`vision_metadata.stage_timings` reports SOM stage timing for debugging:
`image_decode_latency`, `yolo_latency`, `ocr_latency`, `merge_latency`,
`bridge_convert_latency`, `annotation_encode_latency`, `omniparser_latency`,
and `total_latency`. Icon detection confidence is not semantic confidence. Current model provenance
and measured evidence are maintained in `docs/yolo-onnx-poc.md`.

Vision text nodes propagate OCR confidence as `confidence`,
`ocr_confidence`, and `metadata.ocr_confidence`. Default denoising must not
delete visible single-character detections because `+`, `O`, `0`, and Chinese
characters can be valid UI labels. Keep deletion-style rules behind explicit
`CUA_OCR_DENOISE=strict`.

### Deferred Detector Research

Grounding DINO is not a runtime fallback. Consider a separate Windows evaluation
only if measured YOLO recall is insufficient; no extra detector or inference
path should be added without that evidence.

## Learning Pipeline

```
pynput hooks
  -> coalescers
  -> UIA enrichment and sensitive-context gate
  -> LearningSession.actions[]
  -> stop-time OCR enrichment
```

Important behavior:

- Keystrokes and scrolls are coalesced before session storage.
- Password/sensitive fields are redacted or skipped.
- Browser URL detection uses UIA when available.
- Active-window screenshots used for IME/OCR are gated by sensitive context.
- Safety timer stops learning after 600 seconds.

## Critical Constraints

### Performance Is Part of Correctness

`cua-win` is a low-level shared automation runtime, so every modification must
evaluate its performance cost as part of correctness. Account for added
latency, CPU/GPU load, memory, threads/processes, I/O, polling/retries,
screenshot/UIA/MSAA calls, model invocation, startup/warm-up work, and response
payload size. Keep the healthy hot path free of unnecessary waits, duplicate
observations, heavy inference, and background work. Any extra retry or fallback
must be triggered by an observed degraded condition, bounded, and stop as soon
as the required state is available. For meaningful hot-path changes, compare a
representative baseline with the changed path and add deterministic assertions
for applicable limits; do not solve a local reliability problem by imposing an
unmeasured cost on every tool call.

### No First-time C-extension Imports Inside Tool Handlers

Never import `numpy`, `torch`, or trigger any C-extension DLL load for the first
time inside a tool handler. FastMCP stdio/anyio can block C-extension
initialization permanently. Pre-import heavy modules at module scope in
`mcp_server.py` before `create_mcp_server()` returns.

### Return Type Annotations

Do not use `Dict[str, Any]` return annotations on MCP tools with the current MCP
Python SDK. FastMCP infers `outputSchema`, and the SDK validates output before
checking `isError`, which can replace useful errors with:

```text
Output validation error: outputSchema defined but no structured output returned
```

Workaround until the SDK fixes python-sdk#2429/#2447: use `-> Any` on MCP tool
functions. When the SDK is upgraded past the fix:

1. Search `mcp_server.py` for `-> Any:`.
2. Restore structured return types where appropriate.
3. Verify `outputSchema` appears in `tools/list`.
4. Run a negative-path tool test and confirm `isError` responses are preserved.

## Known Limitations

- `detect_sync()` performs synchronous inference in the window-state worker
  thread. SOM warm-up preloads models; first inference and per-frame work still
  contribute to `get_window_state` latency.
- Recording uses a bounded writer thread, but screenshot capture after actions
  still costs 50-200ms per captured turn.
- OCR can read occluded overlapping content if another window covers the target;
  activation mitigates but does not eliminate this.
- PowerShell/CMD have weak UIA ValuePattern support, so learning IME in terminals
  relies on pinyin plus stop-time OCR.
- Replay relocation is best-effort. It can fail when the target window cannot be
  resolved or the UI has changed enough that semantic element matching fails.
- CDP target discovery is cached and refreshed on miss, but each command still
  opens a WebSocket. High-frequency DOM reads may need a connection pool.
- A running `cua-win` daemon keeps code and model state loaded until
  `cua-win stop`, process exit, logout, or reboot. Restart it after code changes.
- `cua-win batch` accepts either a JSON action array or
  `{"defaults": {...}, "actions": [...]}`. Defaults are keyed by tool name and
  shallow-merged into each action; action arguments override defaults. It
  executes mixed MCP calls and waits sequentially through one CLI process and
  one persistent daemon HTTP connection. It is a precomputed batch runner, not
  a server-side script executor; recording still occurs inside each invoked
  action tool. Batch requests suppress the agent cursor trajectory overlay
  while preserving real input timing such as drag duration. Use
  `cua-win batch --dry-run` to validate and summarize expanded actions without
  starting the daemon or sending input.
- `cua-win call TOOL -` reads one UTF-8 JSON argument object from stdin, and
  `cua-win call TOOL @path.json` reads one from a UTF-8 file. Prefer these forms
  for Chinese text or nested JSON to avoid PowerShell/native argument escaping
  and console code-page transformations. When piping Chinese JSON through
  PowerShell, set `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`;
  otherwise PowerShell can replace characters before `cua-win` receives them.
  Inline single-tool arguments also accept a narrow flat primitive shorthand
  such as `{action:status}`. If inline JSON fails, the CLI points users at
  stdin, `@file`, or PowerShell 7+ with a PowerShell-specific hint.
- `cua-win mcp` is the stable stdio MCP product entrypoint. Prefer
  `cua-win mcp-config`, `cua-win mcp-config --client claude`, or
  `cua-win mcp-config --client codex` for client setup instead of hand-writing
  shell-escaped MCP JSON.
- Subprocess environments should set `PYTHONIOENCODING=utf-8` to avoid GBK output
  issues.
- One-shot `cua-win --no-daemon` schema queries and explicitly safe read-only
  calls use `CUA_LIGHTWEIGHT_MCP_IMPORT=1` internally. This skips vision/WGC
  native pre-imports and SOM warm-up. Do not use lightweight mode for
  `computer_get_window_state` or other tools that can enter the vision stack.

## Testing

```bash
uv run pytest -q
python -m compileall -q computer_server som
uv run python scripts/mcp_test_one.py --list
uv run python scripts/wgc_smoke.py
uv run python scripts/run_agent_smoke.py --dry-run --scenario fixture-basic
uv run cua-win computer_get_screen_size --start-timeout 90
uv run cua-win stop
```

## Release Process

- Release artifacts include the Windows installer, wheel, and SHA-256 files.
  The installer is the normal user-facing artifact; the wheel is mainly for
  Python environments and developer validation. Publish them through GitHub
  Releases, not normal PyPI.
- Hybrid standalone builds export the locked production dependency set into an
  isolated staging environment instead of copying the development
  `site-packages`. Pruning removes duplicate RapidOCR default model files but
  must retain the empty `rapidocr/models` directory because RapidOCR validates
  that root before using CUA's explicit bundled model paths.
- Standalone builds must generate UIAutomationCore and oleacc comtypes bindings
  before Nuitka compilation, freeze `comtypes.stream` and the complete generated
  UIAutomation/Accessibility/stdole module set, and assert their module
  definitions in `nuitka-report.xml`. Generated wrappers must not retain
  build-machine typelib mtimes. The standalone checker skips only the
  machine-specific mtime comparison and preserves the comtypes code-generator
  version check. Never depend on build-machine or user comtypes caches.
- Standalone release acceptance must run
  `scripts/validate_standalone_teaching.ps1` against the produced executable
  with private state and APPDATA directories. It must verify Teaching start,
  recording status, stop, idle status, daemon exit, temporary-directory
  cleanup, and zero generated comtypes cache files; `doctor` alone is not a
  sufficient Teaching check.
- Production artifacts contain only the YOLO ONNX and RapidOCR model bundles.
  PyTorch, Torchvision, Ultralytics, Transformers, Tokenizers, and Florence
  resources must be absent. Export/benchmark dependencies stay in the development
  `export` group. Keep the existing standalone support modules until a fresh
  import report justifies removing them; OpenCV FFmpeg and Polars remain excluded.
- `scripts/build_release.ps1` performs the locked install, source/model
  validation, Ruff, tests, compile checks, wheel build, wheel-internal model
  validation, and SHA-256 generation.
- `scripts/build_installer.ps1` builds the normal Windows setup.exe from
  `dist-nuitka/cua-win.dist` using Inno Setup 6. It installs per-user to
  `%LocalAppData%\Programs\cua-win`, creates Start Menu entries, and optionally
  adds `cua-win.exe` to the current user's PATH.
- `uv.lock` is committed release input. Regenerate and commit it whenever
  dependency metadata changes; clean CI checkouts use `uv sync --locked`.
- Manual `Build and release` workflow runs create a one-day checksum candidate;
  they do not publish wheel or installer binaries. A pushed `v*` tag builds the
  wheel and installer, then creates the GitHub Release after the same checks.
- Keep the repository visibility decision separate from release mechanics. The
  repository is currently private and must not be made public implicitly.
- Follow `RELEASE.md` for version updates, clean-environment smoke tests,
  tagging, and model-update requirements.

## Adding A New MCP Tool

1. Add an `@mcp.tool` async function in `create_mcp_server()`.
2. Keep the tool wrapper thin; put business logic into `services/` when it
   composes handlers or owns cross-cutting behavior.
3. Use shared validators from `validation_service.py`.
4. Use `_get_handlers()` for platform operations.
5. If it is an action tool, call `_recording_hook(_recorded_turn(...))` and add
   it to `RECORDABLE_TOOLS`.
6. Preserve `-> Any` return annotations until the MCP SDK outputSchema bug is
   fixed.

## Current Priorities

### Current State

- Protocol `0.2` and package version `0.4.0` are the active development line.
  It intentionally has no 0.1 public compatibility mode. The repository is
  still private.
- Do not move an existing release tag for documentation-only cleanup. Move a
  release tag only when correcting a failed or incomplete release workflow.
- Keep the public MCP surface at 25 tools for now. Do not merge distinct
  high-level operations such as click/drag/scroll, screenshot/window-state/zoom,
  or window discovery/observation/management without concrete agent-usage
  evidence.

### P1: High-Value Development

- Local release smoke has been completed on the development machine. Before
  treating a future installer as broadly validated, repeat the smoke on a clean
  non-development Windows machine: check the installer SHA-256, install
  normally, then run `cua-win doctor`, `cua-win list-tools`, `cua-win start`,
  `cua-win computer_get_screen_size`, one `computer_get_window_state` smoke,
  and `cua-win stop`.
- Keep the repository visibility decision separate from release mechanics. The
  repository must remain private unless the user explicitly chooses to make it
  public.
- Before the next tagged release, update `CHANGELOG.md`, version metadata, and
  `uv.lock` if dependencies changed; then run the full release workflow.

### P2: Useful, But Not Blocking

- Run `scripts/agent_smoke_fixture.py` plus `scripts/run_agent_smoke.py` against
  at least Codex after meaningful tool prompt or skill changes, and save
  transcripts when comparing agent behavior across revisions.
- Check YOLO node localization and main-LLM screenshot interpretation on real
  Windows apps, especially dense toolbars and small controls.
- Periodically run localized Windows app discovery and launch smoke tests on
  real WeChat, DingTalk, and similar Chinese desktop apps.
- Explore broader MSAA-backed actions only when the action contract is
  observable and verification-gated. Dropdown expand-edge click is acceptable
  because it is explicit and observable; broader MSAA default-action dispatch
  needs action-path metadata and post-action verification.
- Continue improving the layered action pipeline only through narrow,
  observable cases. Useful additions are better UIA/MSAA failure reasons,
  fallback-path metadata, and verified secondary actions for specific control
  types; do not add broad accessibility default-action dispatch without
  app/control evidence.
- Run the WGC smoke suite on a real mixed-DPI multi-monitor setup. The
  2026-06-06 controlled run verified occlusion-safe identical frames,
  minimized-window rejection, and 192-DPI primary-monitor capture; Chrome and
  DingTalk live samples completed in roughly 172-219 ms per WGC capture.
  Continue collecting protected-content, GPU-app, WebView2/Electron, and
  mixed-DPI multi-monitor results.

### P3: Defer Until Evidence Accumulates

- Defer `CUA_TOOL_PROFILE=core|full` until the merged 25-tool surface has had
  enough real agent usage to show which low-level primitives should stay hidden
  by default.
- Consider request flags for screenshot, tree, nodes, and vision only if real
  usage shows `computer_get_window_state` response cost or payload size is a
  recurring problem. Current latency does not justify the added API complexity.
- Isolate UIA/MSAA COM work in disposable worker processes only if leaked
  provider calls become an observed operational problem.
- Add a CDP WebSocket connection pool only if browser page tools become
  high-frequency.
- Rework overlay architecture only after more Windows runs expose concrete GDI
  lifecycle, shutdown, or modularity problems.
- Keep YOLO + OCR parallelism disabled by default; the prior EasyOCR CPU speedup
  was about 8%, and RapidOCR needs fresh benchmarks before adding complexity.
- Do not productize a broad background `dispatch` mode yet. Some apps accept
  background clicks but fail to perform follow-up UI transitions, so background
  dispatch can create false success. Revisit only with per-action observability,
  app capability evidence, and verification-gated fallbacks to foreground HID
  input.
- Do not expose PostMessage/background input as a public path without a tested
  compatibility matrix; it is too easy to report success while the target UI
  state remains unchanged.
- Keep the HTTP localhost daemon. A named-pipe daemon is not justified unless
  localhost HTTP becomes a concrete deployment or security blocker.
- Keep the overlay as a single agent cursor. Multi-agent synthetic cursors are
  a visualization feature, not a current reliability requirement.
