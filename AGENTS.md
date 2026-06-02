# AGENTS.md

This file provides guidance to coding agents working in this repository.

# cua-pywin

Windows UI automation MCP server — 43 tools for computer control.

**Priority: Windows + Chinese UI is the highest-guarantee target.**

## Architecture

```
computer_server/
  mcp_server.py             — 43 MCP tools via FastMCP; tool wrappers,
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
cua-win call computer_get_window_state '{"window":{"id":5678,"pid":1234}}'
cua-win stop

# Single-tool tests
uv run python scripts/mcp_test_one.py <tool_name> [json_args] --timeout 120
uv run python scripts/mcp_test_one.py --list

# Compile checks
python -m compileall -q computer_server som
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
`element`. If omitted, legacy inference is preserved: `element_index` means
`element`, zoom args mean `zoom`, `pid + window_id` means `window_resized`,
otherwise `screen`.

`computer_get_window_state` returns `snapshot_id`; element actions can use it
to reject stale observations. `computer_zoom` returns `zoom_id`; zoom actions
can use it instead of implicit per-window zoom state.

Key invariant: `ElementCache` stores window-relative coordinates, and
`_resolve_element_center` returns screen coordinates.

## Window Identity Contract

Public tools should prefer `window` over top-level `pid/window_id`:

```json
{"window":{"id":5678,"pid":1234}}
```

`window.id` and `window.window_id` are equivalent HWND fields. Legacy
`pid/window_id` remains supported for compatibility, but if both forms are
provided they must identify the same target or the tool returns a validation
error. `computer_list_windows`, `computer_get_app_windows`,
`computer_list_apps`, `computer_get_active_window`, `computer_get_window`, and
`computer_get_window_state` return canonical `window` objects for follow-up
calls. Use `computer_get_window({"id":..., "app":...})` to rehydrate a stale or
lost window binding.

`window_ref` is separate replay metadata. Agents should not hand-author it;
recording builds it from the resolved `window` plus origin, process, title,
class, and size metadata.

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
- `computer_get_accessibility_tree` uses the same protected UIA walk path
  instead of calling `walk_uia_tree` synchronously in the tool handler.
- UIA action calls (`InvokePattern.Invoke` and `ValuePattern.SetValue`) run
  behind `UIAActionGate`; timeout or in-flight conflicts fall back to pixel/HID
  paths rather than blocking the action tool indefinitely.
- `CachedElement.platform_ref` is deprecated. Invoke and ValuePattern actions
  resolve a fresh UIA element by point at action time.

### Tree Quality, MSAA, And SOM

`get_window_state` reports UIA, MSAA, and vision quality separately. A bounded
MSAA observation pass runs behind `MSAAWalkGate` alongside UIA because it is
cheap and can preserve legacy roles that UIA proxies collapse. Timed-out MSAA
providers are marked unhealthy by window so repeat requests do not stack leaked
COM calls. MSAA only promotes the combined semantic tree quality when it
contributes usable indexed nodes. SOM vision is used when the combined semantic
tree quality is still poor or empty.

MSAA nodes carry source-specific metadata such as `msaa_role`,
`msaa_role_id`, `msaa_role_hex`, `msaa_default_action`, `msaa_state`,
`msaa_child_id`, and `msaa_has_location`. Dropdown-style MSAA roles
(`BUTTONDROPDOWN`, `BUTTONMENU`, `BUTTONDROPDOWNGRID`, `SPLITBUTTON`) expose
`actions=["invoke","expand"]`; `computer_click(action="expand")` clicks the
right edge of the cached MSAA rect to target the dropdown half.

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
- Legacy `/ws` and `/commands` are removed.
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
  whether the action used UIA Invoke, activation, force-frontmost mouse events,
  MSAA expand-edge click, or normal pixel click.
- `computer_click(verify_query=..., require_verification=True)` performs a
  post-action window-state check and marks the result failed when the query is
  not observed. `pid` and `window_id` are required for verification.
- `capture_after=True` is supported by click, scroll, and type tools and returns
  a fresh snapshot with `snapshot_id`.
- Window snapshots include `capture_metadata` with screenshot provenance,
  crop geometry, resize metadata, capture backend, fallback reason, and
  explicit occlusion-risk warnings. WGC snapshots report
  `capture_backend="wgc"` and `occlusion_safe=True`; fallback screen crops
  report `capture_backend="screen_crop"` and `occlusion_risk=True`.
- `computer_zoom` follows the last snapshot backend: WGC snapshots use WGC for
  native zoom crops and fall back to screen crop only when WGC fails.
- Disabled UIA elements return an explicit error; callers should raise/focus the
  window and re-snapshot.
- `computer_release_all_inputs` releases tracked held keys and mouse buttons
  after interrupted primitive input or replay failures.
- Clipboard typing uses a process-level lock and restores clipboard on type
  failures when possible.
- `computer_page` requires an explicit CDP port or browser
  `--remote-debugging-port`; it does not silently fallback to 9222. JavaScript
  execution requires `allow_javascript=True`; `innerHTML` extraction requires
  `allow_html=True`.

## SOM Vision Strategy

### Current: OmniParser YOLO + EasyOCR + TinyCLIP

```
OCR: EasyOCR default ['ch_sim', 'en'] (Chinese+English)
Icon labels: bundled TinyCLIP wkcn/TinyCLIP-ViT-8M-16-Text-3M-YFCC15M
Perf: CPU 2-5s/frame, GPU <1s/frame
Risk: OmniParser is trained on modern Web/mobile UI; old Win32 recall may vary
```

The OmniParser v2 YOLO detector is bundled at
`som/weights/icon_detect/model.pt` from `microsoft/OmniParser-v2.0`; package
builds include the `som` package and this weight file.

TinyCLIP runs over YOLO icon crops only. The model is bundled under
`computer_server/models/tinyclip/` and package-data includes it for future
builds. It complements EasyOCR: TinyCLIP labels non-text icons; EasyOCR labels
visible text. Inference must remain batched across all icon crops in one pass;
do not call the model once per icon.

Relevant controls:

- `CUA_ENABLE_TINYCLIP=0` disables icon semantic labeling.
- `CUA_TINYCLIP_MODEL` overrides the model id/path; local HF snapshot paths are
  supported.
- `CUA_TINYCLIP_ALLOW_DOWNLOAD=1` allows runtime download. Default is local-only.
- `CUA_TINYCLIP_LABELS` overrides the comma-separated candidate list.
- `CUA_TINYCLIP_MIN_SCORE` and `CUA_TINYCLIP_MIN_MARGIN` tune label acceptance.

`vision_metadata.stage_timings` reports SOM stage timing for debugging:
`image_decode_latency`, `yolo_latency`, `ocr_latency`, `merge_latency`,
`tinyclip_latency`, `bridge_convert_latency`, `annotation_encode_latency`,
`omniparser_latency`, and `total_latency`.

### OCR Fallback: EasyOCR -> PaddleOCR

Use PaddleOCR only if Windows Chinese recognition is insufficient. It is heavier
and has a slower first load.

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
- Subprocess environments should set `PYTHONIOENCODING=utf-8` to avoid GBK output
  issues.

## Testing

```bash
uv run pytest -q
python -m compileall -q computer_server som
uv run python scripts/mcp_test_one.py --list
uv run cua-win computer_get_screen_size --start-timeout 90
uv run cua-win stop
```

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

### P1: High-Value API Stabilization

- Use `computer_get_window` as the public stale-window rehydrate entrypoint
  after lost bindings, agent context resets, or closed/reopened app windows.
- Add screenshot identity for coordinate actions. Coordinates should be able to
  bind to the exact screenshot/crop they came from, building on `snapshot_id`
  and preparing for multi-screenshot state.
- Make `computer_get_window_state` more explicit and cheaper through
  request flags such as screenshot, text/tree, nodes, and vision inclusion.
  Preserve current compatibility defaults until callers migrate.
- Extend `window` input support to keyboard and window-management tools:
  `computer_type`, `computer_press_key`, `computer_hotkey`,
  `computer_activate_window`, `computer_minimize_window`,
  `computer_maximize_window`, and `computer_close_window`.
- Add a `CUA_TOOL_PROFILE=core|full` surface profile. Default should expose a
  smaller high-level core set; full keeps low-level primitives and debug tools.

### P2: Useful, But Not Blocking

- Split non-primary accessibility actions into a dedicated secondary-action
  tool instead of growing `computer_click(action=...)`.
- Continue WGC app coverage testing and capture-stage timing so WGC and screen
  crop can be compared with concrete latency and failure data.
- Deduplicate EnumWindows search strategies for process/title matching.
- Explore broader MSAA-backed actions only when the action contract is
  observable and verification-gated.

### P3: Defer Until Evidence Accumulates

- Isolate UIA/MSAA COM work in disposable worker processes only if leaked
  provider calls become an observed operational problem.
- Add a CDP WebSocket connection pool only if browser page tools become
  high-frequency.
- Rework overlay architecture only after more Windows runs expose concrete GDI
  lifecycle, shutdown, or modularity problems.
- Revisit MobileCLIP-S0 only if TinyCLIP label quality is insufficient on
  collected Windows icon-label samples.
- Keep YOLO + EasyOCR parallelism disabled by default; the measured CPU speedup
  was about 8%, which does not justify added complexity yet.

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
  fallback. Continue testing latency, failure rate, occlusion safety, protected
  content, minimized windows, GPU apps, WebView2/Electron, and multi-monitor DPI
  behavior.
- Keep UIA and MSAA as parallel observation passes. MSAA is cheap enough to run
  alongside UIA, and it preserves legacy roles that UIA providers often lose.
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
- Track capture-stage timing as part of backend work so WGC can be compared
  against screen crop with concrete latency and failure-rate data.

## Future Exploration

- MobileCLIP-S0 is likely a stronger semantic model than TinyCLIP for icon
  labeling, but use the PyTorch/checkpoint path for this Windows Python server.
  The CoreML export is primarily for Apple runtimes and should not be the main
  integration path here. Revisit MobileCLIP-S0 if TinyCLIP label quality is
  insufficient.
- YOLO + EasyOCR `ThreadPoolExecutor` parallelism was evaluated on 2026-06-01
  with a 1280x830 WGC screenshot on CPU: serial averaged 3.31s, parallel
  averaged 3.07s, about 8% faster. Do not enable by default; revisit only with
  representative slow samples or GPU benchmarks.
