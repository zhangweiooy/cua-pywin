# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

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
