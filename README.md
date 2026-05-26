# cua-pywin

Windows UI automation MCP server for AI agents — 54 tools for computer control.
<br>
Windows AI Agent 桌面操控 MCP 服务 — 54 个计算机控制工具。

---

[中文](#中文) | [English](#english)

---

## 中文

### 亮点

- **54 个 MCP 工具** — 截图、鼠标、键盘、剪贴板、文件系统、窗口管理、Shell 执行
- **录制 / 回放** — 录制 MCP 工具调用序列到磁盘，完整回放（含坐标重算和过期窗口解析）
- **学习模式** — 录制真人操作（pynput 全局钩子 + UIA 元素标注 + EasyOCR 视觉增强）
- **SOM 视觉检测** — OmniParser YOLO + EasyOCR 中文识别，UIA 不可用时自动回退
- **UIA 树遍历** — 支持 Win32 / UWP / Electron / Qt 应用，非 UIA 应用自动触发 SOM
- **Agent 光标覆盖层** — Dubins 路径飞行动画 + 弹簧过冲 + 辉光渲染

### 架构

```
cua-pywin/
├─ computer_server/          ← 主程序
│  ├─ mcp_server.py          ← 54 MCP 工具 + 录制/回放/学习入口
│  ├─ main.py                ← 本机 HTTP MCP 服务器
│  ├─ cli.py                 ← CLI 入口
│  ├─ handlers/
│  │  ├─ windows.py          ← Windows 实现：ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM 树遍历 (DFS, 缓存属性)
│  │  ├─ generic.py          ← 跨平台处理器 (文件/桌面/窗口)
│  │  ├─ base.py             ← 抽象接口 (ABCs)
│  │  ├─ element_cache.py    ← 线程安全元素缓存 (pid, window_id)
│  │  ├─ som_bridge.py       ← SOM OmniParser 异步桥接
│  │  ├─ zoom_registry.py    ← 坐标缩放注册表
│  │  └─ browser_cdp.py      ← Chrome DevTools Protocol 客户端
│  ├─ recording/             ← 录制/回放子系统
│  │  ├─ session.py          ← 录制会话 + turn 文件夹写入
│  │  └─ replay.py           ← 轨迹回放引擎
│  ├─ learning/              ← 学习子系统
│  │  ├─ recorder.py         ← 学习录制器 (pynput + UIA + OCR)
│  │  ├─ coalescer.py        ← 按键批次合并 + 滚轮累积
│  │  ├─ platform.py         ← AppDetector / UIAEnricher / Screenshotter / VisionEnricher
│  │  └─ session.py          ← ObservedAction / ElementContext 数据模型
│  └─ overlay/               ← Agent 光标覆盖层
│     ├─ cursor.py           ← 窗口渲染 + 箭头预渲染
│     └─ motion.py           ← Dubins 路径规划 + 弹簧动力学
├─ som/                      ← SOM 视觉 (vendored)
│  ├─ detect.py              ← OmniParser YOLOv8 检测
│  ├─ ocr.py                 ← EasyOCR 中文识别
│  └─ weights/               ← YOLO 模型权重
├─ scripts/
│  └─ mcp_test_one.py        ← 单工具测试脚本
├─ pyproject.toml
└─ README.md
```

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

# HTTP MCP 模式（默认只监听 127.0.0.1，不再提供 legacy /ws API）
python -m computer_server --port 8000
```

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

**屏幕与鼠标** — `computer_screenshot` `computer_click` `computer_move` `computer_drag` `computer_scroll` `computer_mouse_down` `computer_mouse_up` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**键盘** — `computer_type` `computer_press_key` `computer_hotkey` `computer_key_down` `computer_key_up` `computer_set_value`

**剪贴板** — `computer_clipboard_get` `computer_clipboard_set`

**Shell** — `computer_run_command`

**文件系统** — `computer_file_read` `computer_file_write` `computer_file_exists` `computer_directory_exists` `computer_list_directory` `computer_create_directory` `computer_delete_file` `computer_delete_directory` `computer_get_file_size`

**窗口管理** — `computer_open` `computer_launch_app` `computer_get_active_window` `computer_get_window_name` `computer_get_window_size` `computer_get_window_position` `computer_set_window_size` `computer_set_window_position` `computer_activate_window` `computer_minimize_window` `computer_maximize_window` `computer_close_window` `computer_get_app_windows` `computer_list_windows` `computer_list_apps`

**桌面与辅助** — `computer_get_desktop_environment` `computer_set_wallpaper` `computer_get_accessibility_tree` `computer_page`

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

**Per-window 截图（crop，非直接捕获）** — Windows `PrintWindow` 对硬件加速窗口失效。`ImageGrab.grab()` + `GetWindowRect` + PIL `crop()` 是唯一可靠路径。

**UIA 集成** — `get_window_state` 调用 `walk_uia_tree(hwnd)`。UIA COM 要求 STA，在线程执行器中运行。

**树质量 + SOM 回退** — `tree_quality: rich(>50) / ok(15-50)` 跳过 SOM，`poor(<15) / empty(0)` 启用 SOM 视觉检测。

**录制 vs 学习** — 录制是 agent 视角（MCP 工具调用回放），学习是人类视角（pynput + UIA 标注 + OCR 增强）。

**学习 OCR 管道** — 录制中 UIA 读值，失败则截活动窗口。停止时 OCR 识别，全文数据存 `ocr_text`，不自动替换，留给模型分析。

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

- **54 MCP tools** — screenshots, mouse, keyboard, clipboard, file system, window management, shell
- **Recording / Replay** — record MCP tool invocations to disk with full replay (coordinate recalculation + stale-window resolution)
- **Learning mode** — record human actions (pynput global hooks + UIA element enrichment + EasyOCR vision)
- **SOM vision detection** — OmniParser YOLO + EasyOCR (Chinese + English), automatic fallback when UIA is unavailable
- **UIA tree walking** — supports Win32 / UWP / Electron / Qt apps; auto-triggers SOM for non-UIA apps
- **Agent cursor overlay** — Dubins-path flight animation + spring overshoot + glow rendering

### Architecture

```
cua-pywin/
├─ computer_server/          ← Main package
│  ├─ mcp_server.py          ← 54 MCP tools + recording/replay/learning entry points
│  ├─ main.py                ← Local HTTP MCP server
│  ├─ cli.py                 ← CLI entry point
│  ├─ handlers/
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM tree walker (DFS, cached properties)
│  │  ├─ generic.py          ← Cross-platform handlers (file, desktop, window)
│  │  ├─ base.py             ← Abstract interfaces
│  │  ├─ element_cache.py    ← Thread-safe element cache
│  │  ├─ som_bridge.py       ← SOM OmniParser async bridge
│  │  ├─ zoom_registry.py    ← Coordinate zoom registry
│  │  └─ browser_cdp.py      ← Chrome DevTools Protocol client
│  ├─ recording/             ← Recording/replay subsystem
│  │  ├─ session.py          ← Recording session + turn folder writer
│  │  └─ replay.py           ← Trajectory replay engine
│  ├─ learning/              ← Learning subsystem
│  │  ├─ recorder.py         ← Learning recorder (pynput + UIA + OCR)
│  │  ├─ coalescer.py        ← Keystroke buncher + scroll accumulator
│  │  ├─ platform.py         ← AppDetector / UIAEnricher / Screenshotter / VisionEnricher
│  │  └─ session.py          ← ObservedAction / ElementContext data models
│  └─ overlay/               ← Agent cursor overlay
│     ├─ cursor.py           ← Window + arrow pre-renderer
│     └─ motion.py           ← Dubins path planner + spring dynamics
├─ som/                      ← SOM vision (vendored)
│  ├─ detect.py              ← OmniParser YOLOv8 detection
│  ├─ ocr.py                 ← EasyOCR Chinese/English
│  └─ weights/               ← YOLO model weights
├─ scripts/
│  └─ mcp_test_one.py        ← Single-tool test script
├─ pyproject.toml
└─ README.md
```

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

# HTTP MCP mode (binds to 127.0.0.1 by default; legacy /ws API removed)
python -m computer_server --port 8000
```

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

**Screen & Mouse** — `computer_screenshot` `computer_click` `computer_move` `computer_drag` `computer_scroll` `computer_mouse_down` `computer_mouse_up` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**Keyboard** — `computer_type` `computer_press_key` `computer_hotkey` `computer_key_down` `computer_key_up` `computer_set_value`

**Clipboard** — `computer_clipboard_get` `computer_clipboard_set`

**Shell** — `computer_run_command`

**File System** — `computer_file_read` `computer_file_write` `computer_file_exists` `computer_directory_exists` `computer_list_directory` `computer_create_directory` `computer_delete_file` `computer_delete_directory` `computer_get_file_size`

**Window Management** — `computer_open` `computer_launch_app` `computer_get_active_window` `computer_get_window_name` `computer_get_window_size` `computer_get_window_position` `computer_set_window_size` `computer_set_window_position` `computer_activate_window` `computer_minimize_window` `computer_maximize_window` `computer_close_window` `computer_get_app_windows` `computer_list_windows` `computer_list_apps`

**Desktop & Accessibility** — `computer_get_desktop_environment` `computer_set_wallpaper` `computer_get_accessibility_tree` `computer_page`

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

**Per-window screenshot via crop** — `PrintWindow` fails on hardware-accelerated windows. `ImageGrab.grab()` + `GetWindowRect` + PIL `crop()` is the only reliable path.

**UIA integration** — `get_window_state` calls `walk_uia_tree(hwnd)`. UIA COM requires STA, runs in thread executor.

**Tree quality + SOM fallback** — `rich(>50) / ok(15-50)` skip SOM; `poor(<15) / empty(0)` enable SOM vision.

**Recording vs Learning** — Recording is agent-perspective (MCP tool replay); Learning is human-perspective (pynput + UIA + OCR enrichment).

**Learning OCR pipeline** — UIA value probe at record time; active-window screenshot capture on failure. At stop, OCR produces full `ocr_text` with bbox+confidence. Model decides, no auto-replacement.

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
