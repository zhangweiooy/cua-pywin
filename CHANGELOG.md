# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

## 0.3.3 - 2026-09-15

### Changed

- Bound RapidOCR ONNX Runtime sessions to four intra-op threads and one
  inter-op thread.
- Disable the CPU memory arena for YOLO and Florence-2 sessions while retaining
  memory-pattern reuse, reducing retained inference memory without changing
  model outputs.

## 0.3.2 - 2026-09-15

### Changed

- Run the OmniParser icon detector directly through ONNX Runtime CPU with
  NumPy/OpenCV LetterBox, decoding, and deterministic NMS.
- Remove PyTorch, Torchvision, Ultralytics, and EasyOCR from production
  dependencies and release artifacts; retain export tooling only in a
  development dependency group.
- Remove the unused Florence-2 Python processor from the production model
  bundle while retaining its parity fixture in tests.

## 0.3.1 - 2026-09-15

### Changed

- Quantize the OmniParser Florence-2 vision encoder with the official ONNX
  Runtime INT8 pipeline; keep only the Gather-based token embeddings in FP32.
- Use the INT8 vision graph in production and reject stale FP32 vision graphs
  from the model bundle and release validation.

## 0.3.0 - 2026-09-14

### Changed

- Default icon captioning to the locally bundled OmniParser-specialized
  ONNX Community Florence-2 INT8 graphs on ONNX Runtime CPU.
- Report detector/OCR ready before semantic warm-up completes; retain basic
  observations on caption load or inference failures.
- Keep classifier/manual-INT8 PoCs and experimental beam search in development
  scripts, and exclude their resources from release artifacts.

### Fixed

- Respect per-row EOS, generation limits and configured greedy logits rules;
  map decoder KV outputs by name and reject non-finite logits.
- Match official caption tokenizer cleanup and avoid blocking semantic status
  reads behind inference; verify nested model files and reject incomplete hashes.

## 0.2.8 - 2026-08-27

### Fixed

- Load Chromium WebContents accessibility nodes during the first observation
  when the top-level UIA tree is still shell-only.
- Bound degraded Chromium accessibility retries with exponential backoff while
  keeping the ready hot path to a single tree walk.
- Classify UIA tree quality from retained semantic nodes so shell-only trees do
  not incorrectly suppress the existing SOM fallback.

## 0.2.7 - 2026-08-01

### Added

- Detect the bounded WeChat message composition region from its subtle border
  and bottom-toolbar anchors, exposing it as a cached visual edit node.
- Report bounded OCR/icon collision, WeChat composition, and SOM warm-up
  diagnostics without logging screenshots or recognized text.

### Fixed

- Preserve plausible interactive icons when isolated shape-like OCR collides
  with them, while retaining overlapping real text and delaying icon semantics
  to Florence-2.
- Initialize RapidOCR with the explicit bundled model root and prune its entire
  package-default model directory from Nuitka output.
- Keep Florence-captioned icons actionable and preserve icon candidates through
  cross-type suppression when an OCR/icon collision is ambiguous.

## 0.2.6 - 2026-07-30

### Added

- Allow `computer_screenshot` to capture an optional canonical target window
  through the WGC-first, screen-crop-fallback pixel path without running
  UIA/MSAA/SOM or adding a new public MCP tool.
- Add `computer_get_window_state(response_mode="compact")` for bounded
  960-pixel, 30-node observations without verbose capture/provider/vision
  diagnostics.
- Add `cua-win help TOOL` and route `cua-win TOOL --help` to human-readable
  schema-derived tool argument help.

### Changed

- Omit `computer_get_window_state` tree markdown by default; callers can request
  it explicitly with `include_tree_markdown=true`.
- Round public window-state floating-point values to two decimal places while
  retaining full internal observation and coordinate precision.
- Remove exact duplicate window-state fields from public data: keep
  `state_text` as the single summary text, keep canonical node counts, and
  expose capture diagnostics only under `capture.capture_metadata`. Empty
  diagnostic fields are omitted.
- Advanced package, server, workflow, documentation, skill, and release
  metadata to `0.2.6`.
- Kept the public protocol at `0.2` and the public MCP surface at 25 tools.

### Fixed

- Query window process IDs through `win32process` after UIA Invoke and keep
  best-effort window-change telemetry failures from changing a completed click
  into `ACTION_OUTCOME_UNKNOWN`.
- Activate Windows targets by temporarily attaching input threads instead of
  injecting a synthetic Alt keystroke, and always detach successfully attached
  threads when activation completes or fails.

## 0.2.5 - 2026-07-29

### Fixed

- Split coalesced Teaching input by source window and prevent UIA/OCR enrichment
  from reading a different foreground window after focus changes.
- Bind repeated Teaching stop results to their originating session.

### Changed

- Return strict DesktopAction timelines with stable sequence, epoch time, and
  source-window identity plus a bounded stop summary for diagnostics.
- Keep `computer_learn` at the existing public tool while adding session-bound
  start/stop and incremental status arguments.
- Advanced package, server, SOM, workflow, and release metadata to `0.2.5`.
- Kept the public protocol at `0.2` and the public MCP surface at 25 tools.

## 0.2.4 - 2026-07-23

### Fixed

- Make frozen comtypes UIAutomation and Accessibility wrappers portable across
  Windows servicing states by removing build-machine typelib mtimes and
  disabling only the standalone mtime comparison while retaining the comtypes
  code-generator version check.
- Require standalone Teaching acceptance to start and stop a learning session
  from a private state directory instead of relying on dependency-only doctor
  checks.
- Restore `pydoc` and `pydoc_data` because the packaged Florence-2 path loads
  them through Transformers and SciPy, and raise the default RapidOCR timeout
  to 20 seconds for cold CPU inference.

### Changed

- Advanced package, server, SOM, workflow, and release metadata to `0.2.4`.
- Require installer-level clean-environment acceptance plus packaged
  UIA/WGC, repeated screenshot, SOM/OCR/Florence-2, and cleanup checks.
- Kept the public protocol at `0.2` and the public MCP surface at 25 tools.

## 0.2.3 - 2026-07-22

### Fixed

- Make standalone UI Automation independent of developer and user comtypes
  caches by generating UIAutomationCore, Accessibility, and stdole bindings at
  build time, freezing `comtypes.stream` plus the generated modules, and
  validating their presence in the Nuitka report.
- Give the learning UIA health probe a dedicated cold-start timeout while
  retaining the shorter timeout for normal UIA calls.

### Changed

- Advanced package, server, SOM, workflow, and release metadata to `0.2.3`.
- Kept the public protocol at `0.2` and the public MCP surface at 25 tools.

## 0.2.2 - 2026-07-20

### Fixed

- Query UI Automation patterns into their concrete COM interfaces before
  calling `Invoke` or `SetValue`, instead of treating a generic `IUnknown`
  pointer as an actionable pattern.
- Resolve the nearest actionable UIA ancestor when a text node does not expose
  InvokePattern or ValuePattern itself.
- Fall back to pixel click or focused keyboard input when a UIA pattern is
  unavailable before dispatch, and return a stable unsupported-operation error
  instead of leaking COM attribute exceptions.

### Changed

- Advanced package, server, SOM, workflow, and release metadata to `0.2.2`.
- Kept the public protocol at `0.2` and the public MCP surface at 25 tools.
- Build the hybrid standalone from a locked production-only staging
  environment instead of copying the development environment wholesale.
- Prune duplicate RapidOCR defaults, OpenCV's unused FFmpeg backend,
  third-party tests/examples/docs, Polars, and `pydoc` from the standalone.
  (`pydoc` was restored in 0.2.4 after the packaged Florence-2 path proved it
  was a runtime dependency.)

## 0.2.1 - 2026-07-19

### Fixed

- Corrected 0.2 documentation to use `computer_click(count=2)` for
  double-clicks, locate observation summaries under `data`, and describe target
  and transient images through `artifacts[]` rather than a removed
  `data.screenshots` field.
- Refreshed beta/release metadata, the repository MCP config, and the manual
  release workflow default for the 0.2.1 line.
- Corrected public tool descriptions so generated schemas document canonical
  windows, observation and screenshot freshness, action bindings, and
  protocol 0.2 result locations consistently.

### Changed

- Advanced package, server, SOM, workflow, and release metadata to `0.2.1`.

## 0.2.0 - 2026-07-19

### Breaking

- Reset the public MCP protocol to `0.2`. All 25 tools now return the fixed
  `protocol_version/success/data/artifacts/warnings/error/action` envelope for
  successful calls and expected business failures.
- Targeted tools accept only canonical `window: {id, pid}` inputs. Public
  `window_id`, `windowId`, top-level target `pid/window_id`, and compatibility
  aliases have been removed.
- Renamed public `snapshot_id` to `observation_id`. Element actions require the
  latest `observation_id`, screenshot-coordinate actions require the latest
  `screenshot_id`, and zoom-coordinate actions require a current `zoom_id`.
- New observations immediately stale older observation, screenshot, and zoom
  bindings for the same window.
- Recording manifests and turn files now declare protocol `0.2`; replay
  rejects pre-0.2 recordings instead of attempting an implicit migration.

### Added

- Side-effect calls return a transient `action` outcome with `action_id`,
  `completed/rejected/unknown`, and explicit retry safety. No action ledger or
  persistence layer was added.
- Image payloads now live only in MCP image content. Structured
  `artifacts[]` entries bind each image to its content index, dimensions,
  MIME type, byte count, window, and observation identifiers.
- The daemon `/call` transport now returns a fixed
  `{protocol_version,result,content,transport_error}` wrapper. CLI calls read
  the protocol result directly and support `--output` and `--output-dir` for
  image artifacts.
- Stable protocol errors distinguish invalid arguments, missing/mismatched
  targets, missing/stale observations, permissions, unavailable providers,
  capture failures, rejected or unknown actions, and failed postconditions.

### Changed

- Version metadata is advanced to `0.2.0`.
- Post-action capture and query verification share one fresh observation, so
  returned capture bindings are not already stale.
- The recording pipeline writes only canonical public arguments while keeping
  internal HWND/window references for relocation.

### Fixed

- `computer_learn` now owns UI Automation through a dedicated STA worker with
  paired COM initialization and release. Learning startup no longer depends on
  the MCP caller thread's existing COM apartment mode, and signed
  `RPC_E_CHANGED_MODE` HRESULT values are handled correctly.
- Repeated or concurrent `computer_learn(action="stop")` calls now return the
  same completed session without repeating enrichment, evidence writes, or
  duration updates.
- Frozen and daemon runtimes now disable runtime bytecode writes so source-only
  packages in the hybrid standalone cannot add `__pycache__/*.pyc` files after
  staging.
- `cua-win doctor` now treats a clean stopped daemon with no metadata as
  healthy while continuing to warn on stale, unverifiable, or unhealthy state.
- `computer_screenshot` now keeps its MCP image content while also returning
  structured success, format, dimensions, MIME type, and byte-count metadata.

## 0.1.4 - 2026-07-18

### Fixed

- Hardened `cua-win` daemon lifecycle handling so stale state files are validated
  against the recorded PID, daemon command line, and `/status` health before
  being treated as running. Stale metadata is now cleaned during `status` and
  `start`, while read-only doctor checks report it without mutation. Verified
  live-but-unhealthy daemons retain state ownership until an explicit safe
  restart, and process-inspection failures preserve state as unverifiable
  instead of risking a second instance. `stop` only reports success after
  confirmed exit, sends HTTP shutdown only to a health-verified PID, and only
  terminates a process after daemon identity verification on the same process
  instance. Daemon command lines in doctor JSON redact the RPC token.

### Changed

- Version metadata is advanced to `0.1.4`.
- Daemon startup now uses a cross-process lock so concurrent `cua-win start`
  calls converge on one verified daemon instance.

## 0.1.3 - 2026-07-06

### Added

- `computer_learn` now records an additive semantic timeline with
  `observations[]`, `links[]`, correlation ids, observation ids, and session
  metrics while preserving the existing `actions[]` output.
- Learning observations include target, capture, tree-quality, delivery-risk,
  skip, timeout, and error diagnostics for short-session analysis.
- Action results include additive `target_profile`, `delivery_risk`, and
  `delivery_note` metadata in `action_path` for click, type, drag, scroll, and
  key paths.
- Window observations expose richer capture and semantic quality metadata,
  including `capture_reliability`, `semantic_degraded_reason`,
  `degraded_reason`, and `provider_errors`.
- `scripts/windows_robustness_smoke.py` provides a read-only real-app smoke
  summary for Notepad, Edge/Chrome, Windows Terminal, WeChat, and DingTalk.
- Stable `cua-win mcp` and `cua-win mcp-config` entry points simplify MCP client
  setup without hand-written escaped JSON.

### Changed

- Version metadata is advanced to `0.1.3`.
- Windows CLI JSON parsing gives safer guidance for PowerShell, Chinese text,
  stdin, and `@file` argument forms.
- Release and beta handoff docs now emphasize diagnostic fields as additive
  metadata and keep the public MCP surface at 25 tools.

### Compatibility

- No MCP tools were added, removed, or renamed.
- Existing `computer_learn` consumers can continue reading `actions[]`; new
  timeline and quality fields are additive.
- Existing action and observation payloads keep their prior fields while adding
  diagnostics.

### Validation

- Full test baseline: `uv run pytest -q` passed with 266 tests.
- `uv run ruff check .`, `python -m compileall -q computer_server som scripts`,
  and `uv run python scripts/mcp_test_one.py --list` passed.
- Read-only smoke covered Notepad, Edge, Windows Terminal, and WeChat locally;
  DingTalk was unavailable in the no-launch smoke path.

### Security

- Localhost-only defaults, daemon token authentication, restricted CORS, and
  explicit opt-in for CDP JavaScript.
