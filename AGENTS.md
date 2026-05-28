# AGENTS.md

This file provides guidance to coding agents working in this repository.

# cua-pywin

Windows UI automation MCP server — 42 tools for computer control.

**Priority: Windows + Chinese UI is the highest-guarantee target.**

## Architecture

```
computer_server/
  mcp_server.py             — 42 MCP tools via FastMCP; tool wrappers,
                              recording/replay/learning entrypoints
  main.py                   — Shared HTTP app: /status, /mcp, and
                              token-protected cua-win JSON RPC routes
  tool_cli.py               — cua-win CLI, HTTP server launcher, background
                              daemon lifecycle, metadata, and HTTP client
  tool_rpc.py               — Shared tool schema/result JSON serialization
  server_context.py         — Per-instance mutable state: recording session,
                              learning recorder, UIA gate, popover dedup,
                              held input state
  handlers/                 — Platform primitives and OS-facing wrappers
    base.py                 — Abstract handler interfaces
    factory.py              — HandlerFactory + Handlers dataclass
    windows.py              — Windows automation: ImageGrab, win32gui, pynput,
                              clipboard lock, command execution
    windows_uia.py          — UIA COM tree walker
    windows_msaa.py         — MSAA / IAccessible fallback tree walker for
                              classic Win32/VCL/SAL-style controls
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
    window_identity_service.py — window_ref creation and stale HWND resolution
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
cua-win start
cua-win status
cua-win list-tools
cua-win computer_get_screen_size
cua-win call computer_get_window_state '{"pid":1234,"window_id":5678}'
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

## Key Design Decisions

### Per-window Screenshot Via Crop

- `PrintWindow` fails on hardware-accelerated windows such as Chrome and GPU apps.
- The reliable path is full virtual-desktop capture plus `GetWindowRect` crop.
- Window, crop, screenshot origin, and resize ratio are tracked explicitly for
  negative-monitor and partially offscreen windows.

### UIA Integration

- `window_state_service.collect_uia_nodes` calls `handlers.windows_uia.walk_uia_tree`.
- `UIAWalkGate` prevents stacked UIA walks against a hung provider; timed-out
  providers are marked unhealthy briefly and can fall through to SOM.
- `CachedElement.platform_ref` is deprecated. Invoke and ValuePattern actions
  resolve a fresh UIA element by point at action time.

### Tree Quality, MSAA, And SOM

`get_window_state` reports UIA, MSAA, and vision quality separately. A bounded
MSAA observation pass runs alongside UIA because it is cheap and can preserve
legacy roles that UIA proxies collapse. MSAA only promotes the combined semantic
tree quality when it contributes usable indexed nodes. SOM vision is used when
the combined semantic tree quality is still poor or empty.

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
coordinates.

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
- `capture_after=True` is supported by click, scroll, and type tools and returns
  a fresh snapshot with `snapshot_id`.
- Window snapshots include `capture_metadata` with screenshot provenance,
  crop geometry, resize metadata, and explicit occlusion-risk warnings. Current
  window capture is screen-crop based, so covered windows are marked as
  `occlusion_risk=True` until a WGC backend is added.
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

## Deferred Refactors

- Continue monitoring `computer_click` helper boundaries after Windows runs;
  it now separates element dispatch, coordinate resolution, force-frontmost
  click, and pixel click execution.
- EnumWindows search strategies for process/title matching still have duplicated
  closure patterns.
- Review overlay architecture after more Windows runs: GDI thread safety,
  animation shutdown, and pipeline modularity.
- Add a CDP WebSocket connection pool if browser page tools become high-frequency.

## Future Exploration

- MobileCLIP-S0 is likely a stronger semantic model than TinyCLIP for icon
  labeling, but use the PyTorch/checkpoint path for this Windows Python server.
  The CoreML export is primarily for Apple runtimes and should not be the main
  integration path here. Revisit MobileCLIP-S0 if TinyCLIP label quality is
  insufficient.
- Optional later optimization: run YOLO and EasyOCR via `ThreadPoolExecutor` in
  parallel if profiling shows it is worthwhile. Do not add this yet.
