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
                              learning recorder, UIA/MSAA/action gates,
                              popover dedup, held input state
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
python -m computer_server.mcp_server

# Start foreground HTTP server
cua-win serve --port 8000

# Start foreground HTTP server without cua-win JSON RPC routes
cua-win serve --port 8000 --no-cli-rpc

# Background daemon/client flow
cua-win doctor
cua-win start
cua-win status
cua-win list-tools
cua-win computer_get_screen_size
cua-win call computer_get_window_state '{\"window\":{\"id\":5678,\"pid\":1234}}'
cua-win computer_press_key '{\"key\":\"Control_L+s\"}'
cua-win computer_mouse '{\"action\":\"move\",\"x\":100,\"y\":100}'
cua-win computer_recording '{\"action\":\"status\"}'
cua-win computer_learn '{\"action\":\"status\"}'
'[{"type":"wait","milliseconds":100},{"tool":"computer_get_screen_size","arguments":{}}]' | cua-win batch
cua-win stop

# Single-tool tests
uv run python scripts/mcp_test_one.py <tool_name> [json_args] --timeout 120
uv run python scripts/mcp_test_one.py --list

# Compile checks
python -m compileall -q computer_server som

# Validate bundled Codex skill
uv run --no-sync python "C:\Users\vinfl\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills/cua-win

# Build and validate a release candidate
./scripts/build_release.ps1 -Version 0.1.0

# Build a local Nuitka standalone executable directory
./scripts/build_nuitka.ps1

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
`element`. If omitted, default inference is preserved: `element_index` means
`element`, zoom args mean `zoom`, `pid + window_id` means `window_resized`,
otherwise `screen`.

`computer_get_window_state` returns both `snapshot_id` and `screenshot_id`.
`snapshot_id` binds the observation/node snapshot and rejects stale element
observations. `screenshot_id` binds the exact screenshot/crop geometry used for
`window_resized` and `window_full_res` coordinate conversion; prefer passing it
to `computer_click`, `computer_drag`, `computer_scroll`, and `computer_zoom`
when coordinates come from a returned screenshot. `computer_zoom` returns
`zoom_id`; zoom actions can use it instead of implicit per-window zoom state.
Returned `nodes` use `nodes_coordinate_space="window_resized"`, so their
bounding boxes and centers align with the returned target screenshot. By
default, only the 100 highest-priority nodes are returned; set `max_nodes=0` to
return every node or use `query`/`max_nodes` to narrow or expand the payload.
This return limit does not affect the internal `ElementCache`, which continues
to store complete window-full-res coordinates. Nodes include low-level
`actions` plus agent-facing `action_hints` such as `click`, `press`, `focus`,
`type`, `set_value`, `select`, `toggle`, `expand`, and `scroll`.
`accessibility_hint` and top-level `observation_recommendation` summarize
whether UIA/MSAA semantics are sufficient, whether vision/OCR was needed, and
whether callers should narrow with `query` or raise `max_nodes`.

Key invariant: `ElementCache` stores window-relative coordinates, and
`_resolve_element_center` returns screen coordinates.

## Window Identity Contract

Public tools should prefer `window` over top-level `pid/window_id`:

```json
{"window":{"id":5678,"pid":1234}}
```

`window.id` and `window.window_id` are equivalent HWND fields. Top-level
`pid/window_id` remains supported for compatibility, but if both forms are
provided they must identify the same target or the tool returns a validation
error. `computer_list_windows`, `computer_list_apps`, `computer_get_window`,
and `computer_get_window_state` return canonical `window` objects for follow-up
calls. Use `computer_get_window({"id":..., "app":...})` to rehydrate a stale or
lost window binding, and `computer_get_window({"active":true})` for the current
foreground window.

Merged public entry points:

- Hotkeys use `computer_press_key({"keys":[...]})` or
  `computer_press_key({"key":"Control_L+s"})`.
- `computer_type` and `computer_press_key` accept canonical `window`; targeted
  text/key input activates and verifies the target first. Calls without a
  window preserve current-focus behavior, while key-up skips activation so a
  held key can always be released.
- `computer_click`, `computer_drag`, and `computer_scroll` activate and verify
  an explicitly supplied canonical `window` before mouse input. Legacy
  top-level `pid/window_id` calls preserve their prior foreground behavior.
- Keyboard down/up primitives use
  `computer_press_key({"key":"Shift","action":"down|up"})`.
- Mouse move/down/up primitives use `computer_mouse({"action":"move|down|up"})`.
- Window activate/minimize/maximize/close/move/resize use
  `computer_manage_window`.
- Recording start/stop/status uses `computer_recording`.
- Learning start/stop/status uses `computer_learn`.
- App/title/query window search uses `computer_list_windows`.
- Accessibility-tree reads use `computer_get_window_state` `tree_markdown` and
  `nodes`.

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

### Runtime Permissions

- `CUA_DISABLE_CDP_JAVASCRIPT=1` disables CDP JavaScript execution.

## Feature Notes

- `force_frontmost=True` activates a target window and uses `win32api.mouse_event`
  for HID-level clicks needed by some WebView2/UWP targets.
- `computer_click` returns action-path metadata (`method`, foreground/system
  input flags, visibility requirements, coordinate source) so callers can tell
  whether the action used UIA Invoke, force-frontmost mouse events, or normal
  pixel click.
- `computer_get_window_state` returns an additive `screenshots` array. The
  target has `role="target"` and `zIndex=0`; visible related same-process
  menus, popovers, or dialogs can appear as `role="transient"` entries with
  independent `window`, `screenshot_id`, screen origin, capture metadata, and
  positive `zIndex`. Top-level `screenshot` and `screenshot_id` remain the
  target capture for compatibility.
- `computer_perform_secondary_action` is the narrow entry point for
  non-primary node actions. It currently supports only the observable MSAA
  dropdown/splitbutton `expand` path and activates the target before input.
- `computer_click(verify_query=..., require_verification=True)` performs a
  post-action window-state check and marks the result failed when the query is
  not observed. `pid` and `window_id` are required for verification.
- `capture_after=True` is supported by click, scroll, type, and secondary-action
  tools. It returns a fresh snapshot with `snapshot_id`, `screenshot_id`, and
  `capture`; when a pre-action observation is available, it also returns a
  lightweight `post_action_delta` summarizing node count, added/removed nodes,
  text changes, tree-quality changes, and before/after screenshot ids.
- Window snapshots include `capture_metadata` with screenshot provenance,
  crop geometry, resize metadata, capture backend, fallback reason, and
  explicit occlusion-risk warnings. `capture_metadata.stage_timings` includes
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
Icon semantics: bundled OmniParser V2 fine-tuned Florence-2
Perf: CPU 2-5s/frame, GPU <1s/frame
Risk: OmniParser is trained on modern Web/mobile UI; old Win32 recall may vary
```

The OmniParser V2 YOLO detector is bundled at
`som/weights/icon_detect/model.pt` from `microsoft/OmniParser-v2.0`; package
builds include the `som` package and this weight file.

RapidOCR det/cls/rec ONNX models and the Chinese character dictionary are
bundled under `computer_server/models/rapidocr_ppocrv4/`. Runtime initialization
passes these paths explicitly, so OCR does not depend on RapidOCR's package
layout, default model selection, cache, or network download. The ONNX files are
tracked with Git LFS; `cua-win doctor` verifies their SHA-256 hashes.

The fine-tuned Florence-2 icon-caption model, processor, tokenizer, and custom
model code are bundled under
`computer_server/models/omniparser_v2/icon_caption/`. The 1.08 GB
`model.safetensors` is tracked with Git LFS. Runtime model download is disabled
entirely. Missing or invalid model files must fail explicitly; model updates are
made only in the repository and shipped in a new release. `transformers` is
pinned to 4.46.x because later 4.x and 5.x
releases are incompatible with the official OmniParser V2 custom model code.
The resulting wheel is about 1.00 GB and cannot use the normal PyPI file-size
limit; publish it through GitHub Releases, an installer, or another artifact
store. `cua-win doctor` must reject unresolved Git LFS pointer files.

Florence-2 runs over 64x64 YOLO icon crops in batches of up to 64 by default.
Do not call it once per crop. RapidOCR remains responsible for visible text.
TinyCLIP has been removed from the runtime and package after the WeChat
comparison showed materially worse label quality.

### WeChat Icon-Semantic Comparison

The 2026-06-07 comparison used a real WeChat WGC capture collected through
`cua-win`, with 21 YOLO icon crops and 39 OCR text nodes.

Current pipeline timings:

- Total visual pipeline: about 4.01 seconds.
- YOLO: about 0.75 seconds.
- RapidOCR: about 3.16 seconds.
- TinyCLIP: about 0.44 seconds.

Florence-2 icon-caption timings:

- Cold pass: about 0.84 seconds.
- Warm pass: about 0.59-0.60 seconds.
- Process memory: about 1.49 GB during the experiment.

Quality result:

- Florence-2 produced correct or usable descriptions for scissors, more
  options, file/document, square, minimize, close, contacts, messages, and QR
  code icons.
- TinyCLIP accepted only 7 of 21 crops, and most accepted results were incorrect
  `profile button` or `emoji button` labels.
- Florence-2 still misidentified phone, pin, mute, and WeChat-specific icons.
  Generated captions therefore require normalization and an explicit
  low-confidence/unknown path; they must not be treated as ground truth.

Decision: use Florence-2 as the only normal icon-semantic backend. Do not run
TinyCLIP and Florence-2 together. Florence captions remain advisory because
phone, pin, mute, and application-specific icons can still be misidentified.

Relevant controls:

- `CUA_OCR_BACKEND=rapidocr|easyocr` selects OCR backend. RapidOCR is default;
  EasyOCR is optional and requires the `ocr-easy` extra.
- `CUA_OCR_TIMEOUT_SECONDS` overrides per-call OCR timeout.
- `CUA_RAPIDOCR_DEVICE=cpu` is the supported RapidOCR runtime in v1.
- `CUA_RAPIDOCR_MODEL_DIR` is a developer-only local bundle override. It must
  never trigger downloads or become an end-user model update mechanism.
- `CUA_OCR_DENOISE=0` disables OCR text cleanup; `CUA_OCR_DENOISE=strict`
  enables deletion-style filtering for isolated symbol/single-character
  artifacts. Default denoising must stay conservative.
- `CUA_ENABLE_FLORENCE2=0` disables icon semantic captioning.
- `CUA_FLORENCE2_MODEL` and `CUA_FLORENCE2_PROCESSOR` are developer-only local
  directory overrides. Remote repository identifiers are rejected.
- `CUA_FLORENCE2_DEVICE=cpu|cuda` selects the device.
- `CUA_FLORENCE2_BATCH_SIZE` defaults to 64 and is capped at 256.
- `CUA_FLORENCE2_MAX_NEW_TOKENS` defaults to 20 and is capped at 128.

`vision_metadata.stage_timings` reports SOM stage timing for debugging:
`image_decode_latency`, `yolo_latency`, `ocr_latency`, `merge_latency`,
`florence2_latency`, `icon_semantic_latency`, `bridge_convert_latency`,
`annotation_encode_latency`, `omniparser_latency`, and `total_latency`.
`tinyclip_latency=0.0` is retained only as a compatibility field.

Icon nodes preserve `raw_caption`, a conservative `normalized_label`,
`icon_label_source="omniparser_v2_florence2"`, and
`icon_label_confidence_provenance="unscored_greedy_generation"`. Florence
generation does not provide a calibrated confidence, so do not invent one.
`vision_metadata.icon_semantic_status` exposes caption availability. Florence
initialization or inference failure must degrade to unlabeled YOLO icons without
failing the overall SOM observation.

Vision text nodes propagate OCR confidence as `confidence`,
`ocr_confidence`, and `metadata.ocr_confidence`. Default denoising must not
delete visible single-character detections because `+`, `O`, `0`, and Chinese
characters can be valid UI labels. Keep deletion-style rules behind explicit
`CUA_OCR_DENOISE=strict`.

### OCR Fallback: RapidOCR -> EasyOCR

The PP-OCRv4 ONNX files are bundled and loaded through explicit local paths.
Offline/package verification is enforced by `cua-win doctor` and wheel smoke
tests.
Use EasyOCR only as an explicit developer comparison path. EasyOCR runtime
downloads are disabled.

### Detection Fallback: OmniParser -> Grounding DINO

Use Grounding DINO only if OmniParser recall is insufficient. It is more
adaptive but slower on CPU.

Decision: test on Windows first; upgrade only when recall data justifies it.

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

- `detect_sync()` runs synchronously in the tool path. SOM warm-up preloads
  models, but detection still contributes to `get_window_state` tail latency.
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
  and console code-page transformations.
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
uv run cua-win computer_get_screen_size --start-timeout 90
uv run cua-win stop
```

## Release Process

- Release wheels include OmniParser V2 YOLO, Florence-2, and RapidOCR model
  files and are approximately 1 GB. Publish them through GitHub Releases, not
  normal PyPI.
- `scripts/build_release.ps1` performs the locked install, source/model
  validation, Ruff, tests, compile checks, wheel build, wheel-internal model
  validation, and SHA-256 generation.
- `uv.lock` is committed release input. Regenerate and commit it whenever
  dependency metadata changes; clean CI checkouts use `uv sync --locked`.
- Manual `Build and release` workflow runs create a seven-day candidate
  artifact. A pushed `v*` tag creates the GitHub Release after the same checks.
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

### P1: High-Value Development

- Before publishing `v0.1.0`, decide whether the currently private GitHub
  repository should become public, verify the candidate wheel on a second
  Windows machine, finalize `CHANGELOG.md`, and then push the annotated tag.
- Defer `CUA_TOOL_PROFILE=core|full` until the merged 25-tool surface has had
  enough real agent usage to show which low-level primitives should stay hidden
  by default.
- Keep the public surface at 25 tools for now. Do not merge distinct high-level
  operations such as click/drag/scroll, screenshot/window-state/zoom, or
  window discovery/observation/management without concrete agent-usage evidence.

### P2: Useful, But Not Blocking

- Build a reviewed Windows icon-caption corpus for WeChat, DingTalk, Chrome,
  and classic controls. Track Florence incorrect-label and unknown rates,
  especially phone, pin, mute, and application-specific icons.
- Deduplicate EnumWindows search strategies for process/title matching.
- Explore broader MSAA-backed actions only when the action contract is
  observable and verification-gated.
- Run the WGC smoke suite on a real mixed-DPI multi-monitor setup. The
  2026-06-06 controlled run verified occlusion-safe identical frames,
  minimized-window rejection, and 192-DPI primary-monitor capture; Chrome and
  DingTalk live samples completed in roughly 172-219 ms per WGC capture.

### P3: Defer Until Evidence Accumulates

- Consider request flags for screenshot, tree, nodes, and vision only if real
  usage shows `computer_get_window_state` response cost or payload size is a
  recurring problem. Current latency does not justify the added API complexity.
- Isolate UIA/MSAA COM work in disposable worker processes only if leaked
  provider calls become an observed operational problem.
- Add a CDP WebSocket connection pool only if browser page tools become
  high-frequency.
- Rework overlay architecture only after more Windows runs expose concrete GDI
  lifecycle, shutdown, or modularity problems.
- Revisit MobileCLIP-S0 only if Florence-2 memory or package size prevents it
  from becoming the default icon-semantic backend.
- Keep YOLO + OCR parallelism disabled by default; the prior EasyOCR CPU speedup
  was about 8%, and RapidOCR needs fresh benchmarks before adding complexity.

## Deferred Refactors

- Continue monitoring `computer_click` helper boundaries after Windows runs;
  it now separates element dispatch, coordinate resolution, force-frontmost
  click, and pixel click execution.
- EnumWindows search strategies for process/title matching still have duplicated
  closure patterns.
- Review overlay architecture after more Windows runs: GDI thread safety,
  animation shutdown, and pipeline modularity.
- Add a CDP WebSocket connection pool if browser page tools become high-frequency.
- Consider isolating UIA/MSAA COM work in disposable worker processes if leaked
  provider calls become a real operational issue. Current gates stop tool-level
  hangs and repeated stacking, but Python still cannot kill an individual stuck
  cross-process COM call inside the main process.

## Official Approach Follow-ups

- Harden the Windows.Graphics.Capture backend with broader Windows-app testing.
  WGC is now the preferred target-window observation path and screen crop is the
  fallback. `scripts/wgc_smoke.py` now covers controlled occlusion, minimized
  windows, per-monitor DPI capture, and filtered live-app sampling. Continue
  collecting protected-content, GPU-app, WebView2/Electron, and mixed-DPI
  multi-monitor results.
- Keep UIA and MSAA as parallel observation passes. MSAA is cheap enough to run
  alongside UIA, and it preserves classic Win32 roles that UIA providers often lose.
  Continue attaching MSAA-specific metadata to nodes instead of collapsing it
  into generic UIA fields.
- Explore MSAA-backed actions only where the action contract is verifiable.
  Dropdown expand-edge click is acceptable because it is explicit and observable;
  broader MSAA default-action dispatch needs action-path metadata and
  post-action verification before becoming a recommended path.
- Do not productize a broad background `dispatch` mode yet. Some apps accept
  background clicks but fail to perform follow-up UI transitions, so background
  dispatch can create false success. Revisit only with per-action observability,
  app capability evidence, and verification-gated fallbacks to foreground HID
  input.
- Use `capture_metadata.stage_timings` and the WGC smoke report to compare WGC
  against screen crop with concrete latency and failure-rate data.

## Future Exploration

- MobileCLIP-S0 remains a possible lower-memory alternative if Florence-2 is too
  expensive. Use the PyTorch/checkpoint path for this Windows server; CoreML is
  not the primary runtime.
- YOLO + EasyOCR `ThreadPoolExecutor` parallelism was evaluated on 2026-06-01
  with a 1280x830 WGC screenshot on CPU: serial averaged 3.31s, parallel
  averaged 3.07s, about 8% faster. This is historical EasyOCR data; re-benchmark
  with RapidOCR before enabling OCR parallelism.
