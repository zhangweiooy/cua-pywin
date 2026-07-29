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

All tools return protocol `0.2` with fixed
`success/data/artifacts/warnings/error/action` fields. Read tool data from
`data`, errors from `error`, and side-effect certainty from `action`.
Expected failures are normal MCP results. For MCP `isError`,
`ACTION_OUTCOME_UNKNOWN`, or `action.outcome="unknown"`, re-observe before any
retry.

Images are MCP image content and are matched through
`artifacts[].content_index`. CLI callers can save the primary image with
`--output PATH` or all images with `--output-dir DIR`.

## Direct MCP Examples

Get the current foreground window:

```json
{"active": true}
```

Capture a window:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "max_image_dimension": 1280,
  "max_nodes": 100
}
```

`max_nodes` defaults to `100` and returns the highest-priority nodes after any
`query` filtering. Use `0` to return every node.
Each node may include `action_hints`, a compact agent-facing translation of
low-level `actions`. Prefer these hints when choosing between click, type,
set_value, select, toggle, expand, and scroll paths.
`data.accessibility_hint` and `data.observation_recommendation` summarize
whether the semantic tree is usable, whether vision/OCR filled gaps, and whether
to narrow with `query` or increase `max_nodes`.
Read `data.agent_state.text` / `data.state_text` before scanning the full node
list. It is capped by `agent_text_limit=500` by default; pass
`show_full_text=true` or raise `agent_text_limit` for diagnostic runs.

Click a screenshot coordinate:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "screenshot_id": "shot-...",
  "coordinate_space": "window_resized",
  "x": 420,
  "y": 310
}
```

Click an indexed element from the same observation:

```json
{
  "window": {"id": 123456, "pid": 8888},
  "observation_id": "obs-...",
  "element_index": 7
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

## Observe-Plan-Act Pattern

For normal app workflows:

1. Resolve a canonical `window`.
2. Capture state with `computer_get_window_state`.
3. Choose one small state-changing action.
4. Use `capture_after`, `verify_query`, or a fresh state capture.
5. Continue only from the newest `window`, `observation_id`, and
   `screenshot_id`.

Use this loop for navigation, dialogs, search, form entry, menus, tab changes,
focus changes, and any workflow where the next action depends on the previous
result.

Use `cua-win batch` only when the actions are already stable and do not need
intermediate state, such as drawing, repeated drags, deterministic key
sequences, or short low-risk UI setup.

## Targeted Probe Pattern

When a target is ambiguous, inspect a small area instead of re-running broad
window reasoning:

1. Start from a current `screenshot_id`.
2. Call `computer_zoom` around the candidate region.
3. Inspect returned zoom image, nearby nodes, OCR text, and icon labels.
4. Click or type only after the target identity is clear.

If the candidate is text-based, first try `computer_get_window_state` with a
specific `query`. If the query narrows too much, repeat with a higher
`max_nodes` or no query. Use full `max_nodes=0` only for diagnostics.

For tiny or icon-only controls, prefer zoom plus `screenshot_id`/`zoom_id`
coordinate binding over reusing raw screen coordinates.

## High-Risk Workflow

For send, submit, delete, purchase, account, permission, privacy, and security
flows, use this structure:

1. Locate: identify the exact app/window and target context.
2. Prepare: open controls, fill drafts, or stage changes without committing.
3. Verify: confirm active window, recipient/context, pending text/action, and
   submit control from the latest observation or targeted OCR.
4. Commit: perform one final action only after user confirmation when required.
5. Re-verify: confirm the post-action success state or report that the UI did
   not provide reliable confirmation.

Never paste a search token, recipient, or message body unless the latest
observation confirms focus is in the intended input. If text is already present
in the correct target field, do not type it again; submit, finish, or clear and
replace only when the visible value is wrong.

If a UIA action returns success but the visible state does not change, treat it
as inconclusive. Re-observe, then use one current center-coordinate fallback
only when the action is low risk and the target is still clear.

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
