# cua-win Tool Patterns

Load this reference when you need exact tool names, CLI examples, or batch
formats for `cua-pywin`.

## Tool Map

Observation:

- `computer_get_screen_size`
- `computer_get_cursor_position`
- `computer_screenshot`
- `computer_get_window_state`
- `computer_zoom`
- `computer_get_desktop_environment`

Windows and apps:

- `computer_list_apps`
- `computer_list_windows`
- `computer_get_window`
- `computer_launch_app`
- `computer_manage_window`
- `computer_open_url`

Input and controls:

- `computer_click`
- `computer_perform_secondary_action`
- `computer_mouse`
- `computer_drag`
- `computer_scroll`
- `computer_type`
- `computer_press_key`
- `computer_set_value`
- `computer_release_all_inputs`

Browser/CDP:

- `computer_page`

Recording, replay, and learning:

- `computer_recording`
- `computer_replay_trajectory`
- `computer_learn`

## Protocol Envelope

All tools return this Protocol 0.2 envelope; apply the result/recovery rules
in SKILL.md:

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

Images are MCP image content and are matched through
`artifacts[].content_index`. CLI callers can save the primary image with
`--output PATH` or all images with `--output-dir DIR`.

## Direct MCP Examples

Get the current foreground window:

```json
{"active": true}
```

Activate or restore a canonical target window:

```json
{"action": "activate", "window": {"id": 123456, "pid": 8888}}
```

Use this with `computer_manage_window`. On Windows it restores a minimized
target and verifies that it became foreground without injecting synthetic key
input. If activation fails, stop rather than sending untargeted input.

Capture only target-window pixels:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "format": "png",
  "max_image_dimension": 1280
}
```

Use this with `computer_screenshot`. It uses WGC first and a screen-crop
fallback without UIA/MSAA/SOM. The image artifact includes the canonical
window, but this lightweight call does not create `observation_id` or
`screenshot_id`.

Observe a window with actionable semantic and screenshot bindings:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "response_mode": "compact"
}
```

`max_nodes` defaults to `100` and returns the highest-priority nodes after any
`query` filtering. Use `0` to return every node.
`response_mode="compact"` caps the image at 960 pixels, returned nodes at 30,
and the normal state summary at 300 characters while omitting verbose
capture/provider/vision diagnostics. Standard mode preserves the normal
diagnostics and caller-selected size limits.
`tree_markdown` is omitted by default. Pass `include_tree_markdown=true` only
when the full debug tree is required.
Each node may include `action_hints`, a compact agent-facing translation of
low-level `actions`. Prefer these hints when choosing between click, type,
set_value, select, toggle, expand, and scroll paths.
`data.accessibility_hint` and `data.observation_recommendation` summarize
whether the semantic tree is usable, whether vision/OCR filled gaps, and whether
to narrow with `query` or increase `max_nodes`.
Read `data.state_text` before scanning the full node list. `data.agent_state`
contains truncation and sampling metadata without a duplicate text copy. The
summary is capped by `agent_text_limit=500` by default; pass
`show_full_text=true` or raise `agent_text_limit` for diagnostic runs.
Use `include_all_nodes=true` when a query should not restrict the returned
node array. Use `force_som=true` for visual fallback testing or poor UIA/MSAA.

For empty, stale, minimized, or inconsistent observations, inspect
`data.target_profile`, `data.capture.capture_metadata.capture_reliability`,
`data.semantic_degraded_reason`, and `data.provider_errors`.
For action delivery diagnostics, inspect `data.action_path.target_profile`,
`delivery_risk`, and `delivery_note`; these do not indicate an input-mode change.

Click an indexed element from the same observation (preferred):

```json
{
  "window": {"id": 123456, "pid": 8888},
  "observation_id": "obs-...",
  "element_index": 10000
}
```

When no existing node provides the required location, click a screenshot coordinate:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "screenshot_id": "shot-...",
  "coordinate_space": "window_resized",
  "x": 420,
  "y": 310
}
```

Double-click the same indexed element:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "observation_id": "obs-...",
  "element_index": 7,
  "count": 2
}
```

Drag on a canvas:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "screenshot_id": "shot-...",
  "coordinate_space": "window_resized",
  "start_x": 120,
  "start_y": 200,
  "end_x": 420,
  "end_y": 200,
  "duration": 0.05
}
```

Press a chord:

```json
{"window": {"id": 123456, "pid": 8888}, "key": "Control_L+s"}
```

Type literal text:

```json
{"window": {"id": 123456, "pid": 8888}, "text": "hello"}
```

Open and inspect a native context menu:

```json
{"window": {"id": 123456, "pid": 8888}, "key": "Shift_L+F10"}
```

Then call `computer_get_window_state` again and inspect transient image
artifacts/MCP image content or menu nodes before selecting an item.

## Targeted Probe Pattern

For tiny or icon-only controls, first match the target to an existing
`element_index`. Zoom only when the target remains unclear:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "screenshot_id": "shot-...",
  "x1": 400,
  "y1": 280,
  "x2": 460,
  "y2": 340
}
```

Use this with `computer_zoom`; rectangle coordinates come from the resized
parent screenshot. After inspection, prefer the existing node if it matches.
If coordinates must come from the zoom image, use its returned `zoom_id`:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "coordinate_space": "zoom",
  "zoom_id": "zoom-...",
  "x": 30,
  "y": 30
}
```

## CLI Examples

Status and schema:

```powershell
cua-win doctor
cua-win status
cua-win list-tools
cua-win describe computer_get_window_state
cua-win mcp-config
cua-win mcp-config --client claude
cua-win mcp-config --client codex
```

Use generated MCP config commands instead of hand-writing PowerShell-escaped
JSON for client setup.

Use UTF-8 stdin for Chinese text or nested JSON:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
{"query":"微信"}
'@ | cua-win call computer_list_windows -
```

PowerShell may replace Chinese characters before `cua-win` receives them when
`$OutputEncoding` is left at a legacy code page. UTF-8 files with
`cua-win call TOOL @args.json` are also safe.

Implicit tool call:

```powershell
cua-win computer_get_screen_size
```

Daemon lifecycle:

```powershell
cua-win start
cua-win computer_get_screen_size
cua-win stop
```

Avoid `--no-daemon` for heavy tools such as `computer_get_window_state`; it
restarts imports and model warm-up in a one-shot process.

## Batch

Simple batch:

```json
[
  {"tool": "computer_click", "arguments": {"window": {"id": 1, "pid": 2}, "x": 400, "y": 300}},
  {"type": "wait", "milliseconds": 200},
  {"tool": "computer_press_key", "arguments": {"window": {"id": 1, "pid": 2}, "key": "Return"}}
]
```

Batch with per-tool defaults:

```json
{
  "defaults": {
    "computer_drag": {
      "window": {"id": 123456, "pid": 8888},
      "screenshot_id": "shot-...",
      "coordinate_space": "window_resized",
      "duration": 0.02
    }
  },
  "actions": [
    {"tool": "computer_drag", "arguments": {"start_x": 450, "start_y": 300, "end_x": 650, "end_y": 300}},
    {"tool": "computer_drag", "arguments": {"start_x": 450, "start_y": 302, "end_x": 650, "end_y": 302}}
  ]
}
```

Batch suppresses cursor overlay animation while preserving real input timing,
such as drag duration.

Rules:

- `defaults` are keyed by tool name.
- Merge is shallow.
- Action arguments override defaults.
- `wait` entries do not use defaults.
- Execution stops on first failure unless `--continue-on-error` is passed.
- On failure or Ctrl+C, the CLI attempts `computer_release_all_inputs`.
- Use `cua-win batch --dry-run` to validate and summarize expanded actions
  without starting the daemon or sending input.

## Post-Action Observation

Use `capture_after=true` on supported action tools when the next step depends
on visible UI changes. The result includes `data.capture_after`. Add
`include_post_action_delta=true` only when a lightweight before/after comparison
is worth the extra pre-action observation; then the result also includes
`data.post_action_delta`.

`data.post_action_delta` is a lightweight hint, not a full semantic diff. It reports
node-count deltas, added/removed node samples, changed text samples,
tree-quality changes, and before/after observation and screenshot ids. For
high-stakes branching, still inspect `data.capture_after.nodes` or take a fresh
`computer_get_window_state`.

## Recording And Learning

Agent-perspective recording:

```json
{"action": "start", "output_dir": "D:\\temp\\cua-recording"}
```

`output_dir` must be new or empty. Recording start never overwrites existing
contents, and replay rejects any turn whose `protocol_version` is not `0.2`.

```json
{"action": "stop"}
```

Replay:

```json
{"recording_dir": "D:\\temp\\cua-recording", "stop_on_error": true}
```

In 0.4.0, automatic replay rejects recorded elements with `source="vision"`
and `control_type="icon"`, including older references with generated captions.
The rejected step returns `ACTION_REJECTED` before sending its input: neither
the recorded index nor its old position establishes the icon's current identity.
Semantic UIA/MSAA and OCR text relocation are unchanged.

Keep `stop_on_error=true`. On this rejection, inspect the replay result and
current UI; earlier steps may already have completed. Take a fresh
`computer_get_window_state`, identify the target in the full screenshot, zoom
only if needed, then use its current `element_index` and `observation_id`.
Do not restart the entire trajectory blindly, strip recorded identity metadata,
or reuse old coordinates to bypass the rejection. Fresh LLM-guided actions on
YOLO icon nodes remain supported.

Human demonstration learning:

```json
{"action": "start", "session_id": "teaching-session-1", "output_dir": "D:\\temp\\cua-learning", "task_description": "show how to export a file"}
```

Keep the generated `session_id` until stop completes. Start and stop reject a
missing or empty session id. There is no `excluded_window_ids` argument.

For a bounded incremental status read:

```json
{"action": "status", "after_sequence": 0, "timeline_limit": 64}
```

Status returns strict actions whose sequence is greater than
`after_sequence`. Continue from `data.next_sequence`, not from the number of
returned actions. `timeline_limit` must be between 1 and 200. Status fields use
snake_case.

```json
{"action": "stop", "session_id": "teaching-session-1"}
```

Stop returns this session-bound shape:

```json
{
  "sessionId": "teaching-session-1",
  "cutoffSequence": 5,
  "actions": [],
  "summary": {
    "durationSeconds": 12,
    "actionCount": 5,
    "observationCount": 5,
    "degradedObservationCount": 1
  }
}
```

Stop output fields use camelCase. The bounded `summary` does not include full
observations, links, or internal metrics. Repeated or concurrent stop calls
must carry the originating session id and return that completed session rather
than another session's result.

Recording stores MCP tool turns for replay. Learning captures human input with
UIA/OCR enrichment and is for analysis, not direct tool replay.

## CDP

Use `computer_page` only for browsers launched with an explicit
`--remote-debugging-port`.

Required high-privilege flags:

- `execute_javascript` requires `allow_javascript=true`.
- `query_dom` inner HTML extraction requires `allow_html=true`.

Prefer normal window automation for browser UI tasks and CDP for DOM reads or
debugging workflows where the user intentionally enabled remote debugging.
