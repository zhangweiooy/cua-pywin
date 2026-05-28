# cua-pywin

Windows UI automation MCP server for AI agents — 42 tools for computer control.
<br>
Windows AI Agent 桌面操控 MCP 服务 — 42 个计算机控制工具。

---

[中文](#中文) | [English](#english)

---

## 中文

### 亮点

- **42 个 MCP 工具** — 截图、鼠标、键盘、窗口管理
- **录制 / 回放** — 录制 MCP 工具调用序列到磁盘，完整回放（含坐标重算和过期窗口解析）
- **学习模式** — 录制真人操作（pynput 全局钩子 + UIA 元素标注 + EasyOCR 视觉增强）
- **SOM 视觉检测** — OmniParser YOLO + EasyOCR 中文识别 + TinyCLIP 图标语义标签，UIA 不可用时自动回退
- **UIA 树遍历** — 支持 Win32 / UWP / Electron / Qt 应用，非 UIA 应用自动触发 SOM
- **Agent 光标覆盖层** — Dubins 路径飞行动画 + 弹簧过冲 + 辉光渲染

### 架构

```
cua-pywin/
├─ computer_server/          ← 主程序
│  ├─ mcp_server.py          ← 42 MCP 工具入口（薄 wrapper，业务转交 services/）
│  ├─ main.py                ← 本机 HTTP MCP 服务器（/status + /mcp）
│  ├─ cli.py                 ← CLI（非 localhost 需 --i-know-this-exposes-my-computer）
│  ├─ server.py              ← uvicorn 生命周期 + 优雅退出
│  ├─ server_context.py      ← 每实例可变状态（录制会话/学习/UIA gate/popover dedup）
│  ├─ handlers/              ← 平台原语（OS API 一对一封装）
│  │  ├─ base.py             ← 抽象接口 (ABCs)
│  │  ├─ factory.py          ← HandlerFactory + Handlers dataclass
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM 树遍历 (DFS, 缓存属性, 直接 STA 调用)
│  │  ├─ windows_window.py   ← Windows 前台锁绕过专用 handler
│  │  ├─ generic.py          ← 跨平台 desktop / window 辅助 handler
│  │  ├─ element_cache.py    ← (pid, window_id) 元素缓存 + TTL + HWND 有效性
│  │  ├─ zoom_registry.py    ← (pid, window_id) 坐标缩放注册表
│  │  ├─ som_bridge.py       ← SOM OmniParser 桥接 + 解析器状态机
│  │  ├─ browser_cdp.py      ← CDP 客户端（target_id 跟踪 + 2s 缓存 + 失败重试）
│  │  ├─ control_types.py    ← UIA ↔ AX 语义角色统一映射
│  │  ├─ screen_geometry.py  ← VirtualScreenGeometry + DPI 感知
│  │  └─ screenshot_service.py ← 截图 resize / encode 通用工具
│  ├─ services/              ← 应用服务层（mcp_server 的业务从这里跑）
│  │  ├─ coordinate_service.py     ← 坐标/zoom/元素中心解析（纯函数）
│  │  ├─ window_state_service.py   ← WindowSnapshotGeometry + UIAWalkGate +
│  │  │                              get_window_state_snapshot + create_zoom_image
│  │  ├─ window_identity_service.py ← build/resolve_live_window（3 级 fallback）
│  │  ├─ replay_resolver.py        ← 回放时坐标重算 + 过期 HWND 重写
│  │  ├─ capture_service.py        ← virtual desktop 截图（录制 + 学习共享）
│  │  ├─ ocr_service.py            ← lazy OCRProcessor 单例
│  │  ├─ icon_label_service.py     ← TinyCLIP 图标语义标签（batch 推理）
│  │  └─ uia_service.py            ← UIACallGate（semaphore + daemon + timeout）
│  ├─ recording/             ← 录制/回放（agent 视角）
│  │  ├─ session.py          ← 有界 writer queue + 专用 writer 线程
│  │  └─ replay.py           ← 轨迹回放引擎（调用 replay_resolver）
│  ├─ learning/              ← 学习（人类视角）
│  │  ├─ recorder.py         ← pynput hooks + uia_service + ocr_service + 敏感字段闸
│  │  ├─ coalescer.py        ← 按键批次合并 + 滚轮累积
│  │  ├─ platform.py         ← AppDetector / UIAEnricher / Screenshotter / VisionEnricher
│  │  └─ session.py          ← ObservedAction / ElementContext / SECURE_FIELD
│  └─ overlay/               ← Agent 光标覆盖层
│     ├─ cursor.py           ← 单线程持有 HWND + GDI + 消息泵 + 动画循环
│     └─ motion.py           ← Dubins 路径规划 + 弹簧动力学（Phase enum + Pose）
├─ som/                      ← SOM 视觉 (vendored)
│  ├─ detect.py              ← OmniParser YOLOv8 检测
│  ├─ ocr.py                 ← EasyOCR 中文识别
│  └─ weights/               ← 内置 OmniParser v2 YOLO 图标检测权重
├─ scripts/
│  └─ mcp_test_one.py        ← 单工具测试脚本
├─ pyproject.toml
└─ README.md
```

**层依赖方向**：`mcp_server → services → handlers → OS API`。`handlers` 不依赖 `services`；`recording / learning / overlay` 通过 `services` 复用截图、OCR、UIA timeout 能力，不重复实现。

### 安装

```bash
cd D:\Documents\Code\cua-pywin
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

uv：

```bash
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

首次导入自动加载 EasyOCR 模型（~30s），后续秒启动。

### 快速开始

```bash
# MCP stdio 模式（Claude Code / Claude Desktop）
python -m computer_server.mcp_server

# HTTP MCP 模式（默认只监听 127.0.0.1）
python -m computer_server --port 8000

# 若要绑定到非 localhost，必须显式确认（否则启动会被拒绝）
python -m computer_server --host 0.0.0.0 --port 8000 \
    --i-know-this-exposes-my-computer
```

HTTP 服务只暴露 `/status` 与 `/mcp`，legacy `/ws` 与 `/commands` 已删除；所有高权限工具仅通过 MCP 入口暴露。CORS 仅放行 `localhost / 127.0.0.1 / [::1]` origin。

### Claude Code 集成

`.mcp.json`：

```json
{
  "mcpServers": {
    "cua-computer-server": {
      "command": "uv",
      "args": ["run", "python", "-m", "computer_server.mcp_server"],
      "cwd": "D:\\Documents\\Code\\cua-pywin",
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### MCP 工具

**屏幕与鼠标** — `computer_screenshot` `computer_click` `computer_move` `computer_drag` `computer_scroll` `computer_mouse_down` `computer_mouse_up` `computer_release_all_inputs` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**键盘** — `computer_type` `computer_press_key` `computer_hotkey` `computer_key_down` `computer_key_up` `computer_set_value`

**窗口管理** — `computer_open_url` `computer_launch_app` `computer_get_active_window` `computer_get_window_name` `computer_get_window_size` `computer_get_window_position` `computer_set_window_size` `computer_set_window_position` `computer_activate_window` `computer_minimize_window` `computer_maximize_window` `computer_close_window` `computer_get_app_windows` `computer_list_windows` `computer_list_apps`

**桌面与辅助** — `computer_get_desktop_environment` `computer_get_accessibility_tree` `computer_page`

**录制回放** — `computer_set_recording` `computer_get_recording_state` `computer_replay_trajectory`

**学习** — `computer_learn_start` `computer_learn_stop` `computer_learn_status`

### 坐标系统

| 空间 | 描述 |
|---|---|
| 屏幕坐标 | 绝对屏幕位置，`WindowFromPoint` / `GetWindowRect` 使用 |
| 全屏像素 | `screenshot()` 输出，全虚拟桌面 |
| 窗口像素 (全分辨率) | 全屏截图 crop 到窗口 rect 后 |
| 窗口像素 (缩放后) | `get_window_state` 经 `max_image_dimension` 缩放后 |
| Zoom 像素 | `computer_zoom` 裁剪后的全分辨率区域 |

点击坐标传递链路：`element_index → 缓存元素中心 → +窗口原点 → 屏幕坐标 → 缩放 → 实际点击`。

### 关键设计

**Per-window 截图（crop，非直接捕获）** — Windows `PrintWindow` 对硬件加速窗口失效。`ImageGrab.grab()` + `GetWindowRect` + PIL `crop()` 是唯一可靠路径。多显示器负坐标 + 部分屏外通过 `WindowSnapshotGeometry` 显式区分「真实窗口原点」与「可见裁剪原点」。

**UIA 集成 + UIAWalkGate** — 直接在 STA 线程内调用 COM（不再 per-property 提交到全局线程池——那会在 UIA provider 挂死时永久泄漏线程）。`UIAWalkGate`（挂在 `ServerContext`）提供唯一超时边界：30s `wait_for + shield + done_callback`，超时时 gate 保持占用并标记 30s 不健康冷却，后续请求直接走 SOM，不再叠加 doomed walks。

**树质量 + SOM 回退** — `tree_quality: rich(>50) / ok(15-50)` 跳过 SOM，`poor(<15) / empty(0)` 启用 SOM 视觉检测。`uia_quality` 与 `vision_quality` 单独报告，不互相覆盖。

**录制 vs 学习** — 录制是 agent 视角（MCP 工具调用回放），学习是人类视角（pynput + UIA 标注 + OCR 增强）。两者通过 `capture_service / ocr_service / uia_service / window_identity_service` 共享底层能力。

**录制写盘** — 录制 turn 文件夹通过专用 daemon writer 线程 + 有界队列写入，不与 anyio 默认 executor 竞争；队列满时丢弃并计数（`state.dropped_turns`）。

**学习 OCR 管道** — 录制中 UIA 读值，失败则截活动窗口；OCR 截图前用 `_is_sensitive_context` 二级判断（UIA `IsPassword` + 元素 title/identifier 模式匹配），密码或敏感字段跳过截图落盘。停止时 OCR 识别，全文数据存 `ocr_text`，不自动替换，留给模型分析。

**CDP target 跟踪** — `computer_page` 按稳定 `target_id`（CDP tab id）而非 `tab_index` 跟踪页面。`get_page_info` 返回 resolved `target_id`，调用方可用它把后续调用钉到同一 tab；失败重试时按 `target_id` 重解析，不会因 tab 顺序变化漂移到错误页面。

**回放窗口解析** — `_build_window_ref` 保存 `window_id / origin / process_name / window_title / 尺寸`。回放时按以下顺序解析活窗口：(1) 原 HWND 仍有效 → 直接用；(2) 按 `process_name` 找首个可见窗口；(3) 按 `window_title` 子串匹配。click/drag/scroll 也会按 new_origin 重算坐标，不只有 `activate_window`。

### 已知限制

- **SOM 检测同步运行于事件循环线程**：anyio 任务组持有 GIL。warm_up() 启动时预加载（~30s），后续调用 < 3s
- **录制截图必须同步**：`ImageGrab.grab()` 不能卸到线程——anyio 事件循环阻止 `run_in_executor` 任务执行。50 步录制总计 ~5-10s 阻塞
- **SOM OCR 遮挡噪音**：目标窗口被遮挡时拾取重叠内容。`SetForegroundWindow` + 50ms 延迟缓解
- **学习 IME 对终端无效**：PowerShell/CMD 无 UIA ValuePattern，回退拼音 + OCR
- **录制坐标绝对化**：回放时窗口位置变化导致错位。`window_ref` 机制在窗口存活时可重算坐标，窗口过期则按进程名/标题重新搜索
- **GBK 编码**：子进程必须设置 `PYTHONIOENCODING=utf-8`

### 开发

```bash
# 单工具测试
uv run python scripts/mcp_test_one.py <tool_name> [json_args] --timeout 120
uv run python scripts/mcp_test_one.py --list

# 编译检查
python -m py_compile computer_server/*.py
python -m py_compile computer_server/handlers/*.py
python -m py_compile computer_server/recording/*.py
python -m py_compile computer_server/learning/*.py
```

---

## English

### Highlights

- **42 MCP tools** — screenshots, mouse, keyboard, window management
- **Recording / Replay** — record MCP tool invocations to disk with full replay (coordinate recalculation + stale-window resolution)
- **Learning mode** — record human actions (pynput global hooks + UIA element enrichment + EasyOCR vision)
- **SOM vision detection** — OmniParser YOLO + EasyOCR (Chinese + English) + TinyCLIP icon labels, automatic fallback when UIA is unavailable
- **UIA tree walking** — supports Win32 / UWP / Electron / Qt apps; auto-triggers SOM for non-UIA apps
- **Agent cursor overlay** — Dubins-path flight animation + spring overshoot + glow rendering

### Architecture

```
cua-pywin/
├─ computer_server/          ← Main package
│  ├─ mcp_server.py          ← 42 MCP tools (thin wrappers; logic in services/)
│  ├─ main.py                ← Local HTTP MCP server (/status + /mcp only)
│  ├─ cli.py                 ← CLI (non-localhost requires --i-know-this-exposes-my-computer)
│  ├─ server.py              ← uvicorn lifecycle + graceful shutdown
│  ├─ server_context.py      ← Per-instance state (recording / learning /
│  │                          UIA walk gate / popover dedup)
│  ├─ handlers/              ← Platform primitives (1:1 OS API wrappers)
│  │  ├─ base.py             ← Abstract interfaces (ABCs)
│  │  ├─ factory.py          ← HandlerFactory + Handlers dataclass
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM tree walker (DFS, direct STA calls)
│  │  ├─ windows_window.py   ← Windows foreground-lock bypass
│  │  ├─ generic.py          ← Cross-platform desktop / window helper handler
│  │  ├─ element_cache.py    ← (pid, window_id) cache + TTL + HWND validity
│  │  ├─ zoom_registry.py    ← Per-(pid, window_id) zoom translation
│  │  ├─ som_bridge.py       ← SOM OmniParser bridge + parser state machine
│  │  ├─ browser_cdp.py      ← CDP client (target_id tracking + 2s cache)
│  │  ├─ control_types.py    ← Unified UIA ↔ AX role mapping
│  │  ├─ screen_geometry.py  ← VirtualScreenGeometry + DPI awareness
│  │  └─ screenshot_service.py ← Resize / encode helpers
│  ├─ services/              ← Application service layer
│  │  ├─ coordinate_service.py     ← coords / zoom / element center (pure)
│  │  ├─ window_state_service.py   ← WindowSnapshotGeometry + UIAWalkGate
│  │  │                              + get_window_state_snapshot + create_zoom_image
│  │  ├─ window_identity_service.py ← build/resolve_live_window (3-level)
│  │  ├─ replay_resolver.py        ← coord rewrite + stale HWND rewrite
│  │  ├─ capture_service.py        ← virtual-desktop mss capture
│  │  ├─ ocr_service.py            ← lazy OCRProcessor singleton
│  │  ├─ icon_label_service.py     ← TinyCLIP icon labels (batched inference)
│  │  └─ uia_service.py            ← UIACallGate (semaphore + daemon + timeout)
│  ├─ recording/             ← Agent-perspective recording / replay
│  │  ├─ session.py          ← Bounded writer queue + dedicated writer thread
│  │  └─ replay.py           ← Trajectory replay engine
│  ├─ learning/              ← Human-perspective recording
│  │  ├─ recorder.py         ← pynput hooks + uia_service + ocr_service
│  │  ├─ coalescer.py        ← Keystroke buncher + scroll accumulator
│  │  ├─ platform.py         ← AppDetector / UIAEnricher / Screenshotter / VisionEnricher
│  │  └─ session.py          ← ObservedAction / ElementContext / SECURE_FIELD
│  └─ overlay/               ← Agent cursor overlay
│     ├─ cursor.py           ← Single thread owns HWND + GDI + msg pump
│     └─ motion.py           ← Dubins planner + spring (Phase enum + Pose)
├─ som/                      ← SOM vision (vendored)
│  ├─ detect.py              ← OmniParser YOLOv8 detection
│  ├─ ocr.py                 ← EasyOCR Chinese/English
│  └─ weights/               ← Bundled OmniParser v2 YOLO icon detector
├─ scripts/
│  └─ mcp_test_one.py        ← Single-tool test script
├─ pyproject.toml
└─ README.md
```

**Layer direction**: `mcp_server → services → handlers → OS APIs`. `handlers` never imports `services`. `recording`, `learning`, and `overlay` share capture / OCR / UIA-timeout behaviour through `services` rather than duplicating it.

### Installation

```bash
cd D:\Documents\Code\cua-pywin
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

Or with uv:

```bash
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

First import loads EasyOCR models (~30s). Subsequent starts are instant.

### Quick Start

```bash
# MCP stdio mode (Claude Code / Claude Desktop)
python -m computer_server.mcp_server

# HTTP MCP mode (binds 127.0.0.1 by default)
python -m computer_server --port 8000

# Binding to a non-localhost host requires explicit consent — without it
# the server refuses to start.
python -m computer_server --host 0.0.0.0 --port 8000 \
    --i-know-this-exposes-my-computer
```

The HTTP server exposes only `/status` and `/mcp`; the legacy `/ws` and `/commands` WebSocket APIs have been removed. All high-privilege control flows through the MCP tool registry. CORS only allows `localhost / 127.0.0.1 / [::1]` origins.

### Claude Code Integration

`.mcp.json`:

```json
{
  "mcpServers": {
    "cua-computer-server": {
      "command": "uv",
      "args": ["run", "python", "-m", "computer_server.mcp_server"],
      "cwd": "D:\\Documents\\Code\\cua-pywin",
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### MCP Tools

**Screen & Mouse** — `computer_screenshot` `computer_click` `computer_move` `computer_drag` `computer_scroll` `computer_mouse_down` `computer_mouse_up` `computer_release_all_inputs` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**Keyboard** — `computer_type` `computer_press_key` `computer_hotkey` `computer_key_down` `computer_key_up` `computer_set_value`

**Window Management** — `computer_open_url` `computer_launch_app` `computer_get_active_window` `computer_get_window_name` `computer_get_window_size` `computer_get_window_position` `computer_set_window_size` `computer_set_window_position` `computer_activate_window` `computer_minimize_window` `computer_maximize_window` `computer_close_window` `computer_get_app_windows` `computer_list_windows` `computer_list_apps`

**Desktop & Accessibility** — `computer_get_desktop_environment` `computer_get_accessibility_tree` `computer_page`

**Recording & Replay** — `computer_set_recording` `computer_get_recording_state` `computer_replay_trajectory`

**Learning** — `computer_learn_start` `computer_learn_stop` `computer_learn_status`

### Coordinate Systems

| Space | Description |
|---|---|
| Screen coords | Absolute screen position, used by `WindowFromPoint` / `GetWindowRect` |
| Full-screen pixels | `screenshot()` output, full virtual desktop |
| Window pixels (full-res) | After cropping full-screen shot to window rect |
| Window pixels (resized) | After `max_image_dimension` downscale in `get_window_state` |
| Zoom pixels | Full-resolution region cropped by `computer_zoom` |

Click coordinate pipeline: `element_index → cached element center → +window origin → screen coords → scale → actual click`.

### Key Design Decisions

**Per-window screenshot via crop** — `PrintWindow` fails on hardware-accelerated windows. `ImageGrab.grab()` + `GetWindowRect` + PIL `crop()` is the only reliable path. Multi-monitor negative coordinates and partially-offscreen windows are handled by `WindowSnapshotGeometry`, which explicitly separates the real window origin from the visible crop origin.

**UIA integration + UIAWalkGate** — COM is called directly inside its STA thread (the previous design submitted each property call to a shared executor and leaked threads when the UIA provider hung). `UIAWalkGate` (owned by `ServerContext`) is the sole timeout boundary: 30s `wait_for + shield + done_callback`. On timeout the gate stays held and the key is marked unhealthy for 30s, so fresh requests fall straight through to SOM instead of stacking more doomed walks.

**Tree quality + SOM fallback** — `rich(>50) / ok(15-50)` skip SOM; `poor(<15) / empty(0)` enable SOM vision. `uia_quality` and `vision_quality` are reported separately and do not overwrite each other.

**Bundled OmniParser v2 detector** — The YOLO icon detector is bundled at
`som/weights/icon_detect/model.pt` from `microsoft/OmniParser-v2.0`, so packaged
installs do not need to download it at runtime.

**TinyCLIP icon labels** — SOM icon crops are labeled in one TinyCLIP batch.
The model (`wkcn/TinyCLIP-ViT-8M-16-Text-3M-YFCC15M`) is bundled under
`computer_server/models/tinyclip/` and merged into vision nodes as `name`,
`icon_label`, and `metadata.icon_label_*`. Runtime download is disabled by
default; use `CUA_TINYCLIP_MODEL` to override the local model path or
`CUA_TINYCLIP_ALLOW_DOWNLOAD=1` to allow Hugging Face downloads. SOM debug
timing is exposed under `vision_metadata.stage_timings`, including YOLO, OCR,
TinyCLIP, merge, encode, and total latency.

**Recording vs Learning** — Recording is agent-perspective (MCP tool replay); Learning is human-perspective (pynput + UIA + OCR enrichment). The two share capture / OCR / UIA-timeout / window-identity through `services/` rather than duplicating logic.

**Recording disk writes** — Turn-folder writes go through a dedicated daemon writer thread + bounded queue so they don't compete with the anyio default executor; full queue drops with a counter in `state.dropped_turns`.

**Learning OCR pipeline** — UIA value probe at record time; active-window screenshot on failure. Before any OCR screenshot lands on disk, `_is_sensitive_context` gates it (UIA `IsPassword` + element title/identifier pattern match) so password / sensitive fields are skipped. At stop, OCR produces full `ocr_text` with bbox+confidence. Model decides, no auto-replacement.

**CDP target tracking** — `computer_page` tracks pages by stable `target_id` (CDP tab id) rather than `tab_index`. `get_page_info` returns the resolved `target_id` so callers can pin subsequent calls to the same tab; retries re-resolve by `target_id`, never drifting to the wrong page when tab order changes.

**Replay window resolution** — `_build_window_ref` stores `window_id / origin / process_name / window_title / size`. At replay time the live window is resolved in this order: (1) original HWND if still valid, (2) first visible window matching `process_name`, (3) substring match on `window_title`. click / drag / scroll also rewrite coordinates against the new origin — not just `activate_window`.

### Known Limitations

- **SOM detection runs synchronously on the event loop**: anyio task group holds the GIL. warm_up() pre-loads at startup (~30s); subsequent calls <3s
- **Recording screenshots cannot be offloaded**: `ImageGrab.grab()` must stay synchronous — anyio blocks `run_in_executor`. ~5-10s total for 50-action recordings
- **SOM OCR occlusion noise**: occluded windows pick up overlapping content. Mitigated by `SetForegroundWindow` + 50ms delay
- **Learning IME not supported in terminals**: PowerShell/CMD lack UIA ValuePattern. Falls back to pinyin + OCR
- **Recording coordinates are absolute**: window position changes break replay. `window_ref` recalculates if window is alive; falls back to process-name/title search for stale IDs
- **GBK encoding**: set `PYTHONIOENCODING=utf-8` in subprocess environments

### Development

```bash
# Single-tool test
uv run python scripts/mcp_test_one.py <tool_name> [json_args] --timeout 120
uv run python scripts/mcp_test_one.py --list

# Compile check
python -m py_compile computer_server/*.py
python -m py_compile computer_server/handlers/*.py
python -m py_compile computer_server/recording/*.py
python -m py_compile computer_server/learning/*.py
```
