# cua-pywin

Windows UI automation MCP server for AI agents — 25 tools for computer control.
<br>
Windows AI Agent 桌面操控 MCP 服务 — 25 个计算机控制工具。

---

[中文](#中文) | [English](#english)

---

## 中文

### 亮点

- **25 个 MCP 工具** — 截图、鼠标、键盘、窗口管理；重复 wrapper 已合并为高层入口
- **录制 / 回放** — 录制 MCP 工具调用序列到磁盘，完整回放（含坐标重算和过期窗口解析）
- **学习模式** — 录制真人操作（pynput 全局钩子 + UIA 元素标注 + PaddleOCR ONNX 视觉增强）
- **SOM 视觉检测** — OmniParser YOLO + PaddleOCR ONNX 中文识别 + TinyCLIP 图标语义标签，UIA 不可用时自动回退
- **UIA 树遍历** — 支持 Win32 / UWP / Electron / Qt 应用，非 UIA 应用自动触发 SOM
- **Agent 光标覆盖层** — Dubins 路径飞行动画 + 弹簧过冲 + 辉光渲染

### 架构

```
cua-pywin/
├─ computer_server/          ← 主程序
│  ├─ mcp_server.py          ← 25 MCP 工具入口（薄 wrapper，业务转交 services/）
│  ├─ main.py                ← 本机 HTTP 服务器（/status + /mcp + cua-win JSON RPC）
│  ├─ tool_cli.py            ← cua-win CLI / HTTP server launcher
│  ├─ server_context.py      ← 每实例可变状态（录制会话/学习/UIA gate/popover dedup）
│  ├─ handlers/              ← 平台原语（OS API 一对一封装）
│  │  ├─ base.py             ← 抽象接口 (ABCs)
│  │  ├─ factory.py          ← HandlerFactory + Handlers dataclass
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM 树遍历 (DFS, 缓存属性, 直接 STA 调用)
│  │  ├─ windows_wgc.py      ← Windows.Graphics.Capture 目标窗口捕获
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
│  ├─ ocr.py                 ← PaddleOCR ONNX / RapidOCR 中文识别
│  └─ weights/               ← 内置 OmniParser v2 YOLO 图标检测权重
├─ scripts/
│  ├─ mcp_test_one.py        ← 单工具测试脚本
│  └─ wgc_smoke.py           ← WGC 遮挡/最小化/显示器 DPI/真实应用 smoke
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

默认 OCR 后端是 RapidOCR + ONNXRuntime（PaddleOCR PP-OCRv4 mobile 模型）。
首次初始化会校验/下载 RapidOCR ONNX 模型；后续 daemon 热启动复用已加载状态。
OCR 文本节点会保留 `confidence`。默认去噪只做空文本过滤和确定性误读修正；
删除孤立符号/单字符的严格过滤需显式设置 `CUA_OCR_DENOISE=strict`。

### 快速开始

```bash
# MCP stdio 模式（Claude Code / Claude Desktop）
python -m computer_server.mcp_server

# 统一 HTTP 服务（默认只监听 127.0.0.1，提供 /status + /mcp + token JSON RPC）
cua-win serve --port 8000

# CLI daemon/client（默认自动启动本机 HTTP daemon，无需手动 serve）
cua-win doctor
cua-win status
cua-win list-tools
cua-win computer_get_screen_size
@'
{"window":{"id":5678,"pid":1234}}
'@ | cua-win call computer_get_window_state -
cua-win computer_press_key '{\"key\":\"Control_L+s\"}'
cua-win computer_mouse '{\"action\":\"move\",\"x\":100,\"y\":100}'
cua-win computer_recording '{\"action\":\"status\"}'
cua-win computer_learn '{\"action\":\"status\"}'
$batch = '[{"type":"wait","milliseconds":100},{"tool":"computer_get_screen_size","arguments":{}}]'
$batch | cua-win batch
cua-win stop

# 若要绑定到非 localhost，必须显式确认（否则启动会被拒绝）
cua-win serve --host 0.0.0.0 --port 8000 \
    --i-know-this-exposes-my-computer
```

普通 HTTP 服务暴露 `/status` 与 `/mcp`；`cua-win` daemon 复用同一个 app，并额外启用 token 保护的 `/tools`、`/call`、`/shutdown` JSON RPC 路由。CORS 仅放行 `localhost / 127.0.0.1 / [::1]` origin。

`cua-win` 是面向人和脚本的 CLI。默认启动同一个本机 HTTP app，并通过 token 保护的 JSON RPC 路由调用 MCP 工具，因此 `computer_get_window_state` 这类高频重工具只在 daemon 启动时预热一次。`--no-daemon` 会强制单次进程内调用；`list-tools`、`describe` 和明确安全的只读状态调用使用轻量导入，不会启动 SOM/TinyCLIP/RapidOCR 预热。桌面控制和视觉调用仍推荐复用 daemon。
`cua-win <tool> '<json>'` 会自动等价转换为 `cua-win call <tool> '<json>'`。
包含中文或复杂嵌套参数时，推荐通过 UTF-8 stdin
（`'<json>' | cua-win call <tool> -`）或 UTF-8 文件
（`cua-win call <tool> '@arguments.json'`）传入，避免 PowerShell/native
参数转义和控制台代码页改变内容。内联 JSON 仍保持兼容。

`cua-win batch` 从标准输入、内联 JSON 或 UTF-8 JSON 文件读取动作数组，或
`{"defaults":...,"actions":[...]}` 对象，并在同一 CLI 进程中通过常驻 daemon
顺序执行。条目可以是
`{"tool":"computer_click","arguments":{...}}`，也可以是
`{"type":"wait","milliseconds":200}`。默认首个失败即停止；使用
`--continue-on-error` 可继续后续条目。失败或 Ctrl+C 中断时会尝试调用
`computer_release_all_inputs`。批处理调用会关闭 agent 鼠标轨迹动画，避免大量连续
动作在 overlay 动画上产生额外等待；真实鼠标拖拽的 `duration` 仍按动作参数执行。

`computer_launch_app` 默认只报告进程启动结果。需要立即执行窗口操作时可传
`{"wait_for_window":true,"wait_timeout":10}`；工具会等待可定位的顶层窗口并返回
canonical `window`。微信等应用可能复用已有进程，或由不同于 launcher PID 的进程
持有主窗口，因此应以返回的 `window.pid/window.id` 为准。等待超时不会否定已经
成功的应用启动，而会明确返回 `window_ready=false`。

大量同类操作可用按工具 defaults 减少重复参数。合并是浅合并，单条动作参数优先：

```json
{
  "defaults": {
    "computer_drag": {
      "window": {"id": 123456, "pid": 8888},
      "screenshot_id": "shot-123",
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

`cua-win doctor` 执行只读本机诊断：平台、Python 版本、依赖可发现性、打包模型文件、状态目录和现有 daemon 可达性。它不会启动 daemon 或调用桌面控制工具。

如果只需要 `/status + /mcp`，可用 `cua-win serve --no-cli-rpc` 关闭 CLI JSON RPC 路由。

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

**屏幕与鼠标** — `computer_screenshot` `computer_click` `computer_perform_secondary_action` `computer_mouse` `computer_drag` `computer_scroll` `computer_release_all_inputs` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**键盘** — `computer_type` `computer_press_key` `computer_set_value`

**窗口与应用** — `computer_open_url` `computer_launch_app` `computer_get_window` `computer_manage_window` `computer_list_windows` `computer_list_apps`

**桌面与浏览器** — `computer_get_desktop_environment` `computer_page`

**录制回放** — `computer_recording` `computer_replay_trajectory`

**学习** — `computer_learn`

### 坐标系统

| 空间 | 描述 |
|---|---|
| 屏幕坐标 | 绝对屏幕位置，`WindowFromPoint` / `GetWindowRect` 使用 |
| 全屏像素 | `screenshot()` 输出，全虚拟桌面 |
| 窗口像素 (全分辨率) | 全屏截图 crop 到窗口 rect 后 |
| 窗口像素 (缩放后) | `get_window_state` 经 `max_image_dimension` 缩放后 |
| Zoom 像素 | `computer_zoom` 裁剪后的全分辨率区域 |

点击坐标传递链路：`element_index → 缓存元素中心 → +窗口原点 → 屏幕坐标 → 缩放 → 实际点击`。

### Window 契约

推荐使用 `window` 对象串联窗口发现、观察和动作工具：

```json
{"window":{"id":5678,"pid":1234}}
```

`window.id` 与 `window.window_id` 都表示 HWND。旧的顶层
`pid/window_id` 仍兼容，但与 `window` 同时传入时必须一致，否则工具会
返回 validation error。`computer_list_windows`、`computer_list_apps`、
`computer_get_window` 和 `computer_get_window_state` 都会返回可直接复用的
`window` 对象。使用 `computer_get_window({"id":...,"app":...})` 可以在 stale
handle、绑定丢失或上下文重置后重新获取当前窗口对象；使用
`computer_get_window({"active":true})` 获取当前前台窗口。
`window_ref` 仅用于 recording/replay 的内部窗口身份快照，不要求调用方构造。
`computer_type` 和 `computer_press_key` 也支持 canonical `window`。传入目标
窗口时，会先激活并验证前台窗口，再发送文本、热键、按键或 key-down；不传
`window/window_id` 时继续作用于当前焦点。key-up 为保证能释放全局按键状态，
不会重新激活目标窗口。
`computer_click`、`computer_drag` 和 `computer_scroll` 在显式传入 `window`
时也会先激活并验证目标窗口；激活失败时不会发送鼠标输入。仅使用旧的顶层
`pid/window_id` 时保留原行为，不隐式改变前台窗口。

合并后的入口约定：热键通过 `computer_press_key({"keys":[...]})` 或
`computer_press_key({"key":"Control_L+s"})` 执行；窗口激活、最小化、最大化、
关闭、移动和缩放统一走 `computer_manage_window`；按 app/title/query 查窗口统一走
`computer_list_windows`；鼠标 move/down/up 低层动作统一走
`computer_mouse({"action":"move|down|up"})`；键盘 down/up 低层动作统一走
`computer_press_key({"key":"Shift","action":"down|up"})`；录制 start/stop/status
统一走 `computer_recording({"action":"start|stop|status"})`；学习 start/stop/status
统一走 `computer_learn({"action":"start|stop|status"})`；辅助树读取统一走
`computer_get_window_state` 的 `tree_markdown` 和 `nodes`。
节点的主动作使用 `computer_click`；节点明确暴露的非主动作使用
`computer_perform_secondary_action`。当前仅支持 MSAA dropdown/splitbutton 的
`action="expand"`。窗口置前不属于 accessibility action，统一使用
`computer_manage_window({"action":"activate",...})`。

坐标链路推荐使用 `screenshot_id`。`computer_get_window_state` 同时返回
`snapshot_id` 和 `screenshot_id`：`snapshot_id` 绑定观察/节点快照，
`screenshot_id` 绑定该次截图的 crop origin、resize ratio 和 capture backend。
当 `computer_click`、`computer_drag`、`computer_scroll` 或 `computer_zoom` 的坐标来自
返回截图时，传入 `screenshot_id` 可以避免后续截图刷新覆盖全局几何状态。
返回的 `nodes` 使用 `nodes_coordinate_space="window_resized"`，其 `bbox`
和中心坐标可直接与主截图对齐；内部 `ElementCache` 仍保留窗口全分辨率坐标。
`computer_get_window_state` 还返回 `screenshots` 数组：第一项是
`role="target", zIndex=0` 的主截图；当前可见的同应用关联菜单、弹层或对话框会
作为 `role="transient"` 项返回。每项都有独立的 `window`、`screenshot_id`、
屏幕原点和 `zIndex`，可直接用于后续窗口坐标动作。旧的顶层 `screenshot`
与 `screenshot_id` 字段继续表示主截图。

### 关键设计

**Per-window 截图** — Windows `PrintWindow` 对硬件加速窗口失效。`get_window_state` 优先使用 Windows.Graphics.Capture 捕获目标窗口自身的 DWM 合成帧，避免被前景遮挡窗口污染；失败时回退到 `ImageGrab.grab()` + `GetWindowRect` + PIL `crop()`。`computer_zoom` 会沿用上一次 snapshot backend：WGC snapshot 使用 WGC 做原生 zoom crop，失败才退回 screen crop。多显示器负坐标 + 部分屏外通过 `WindowSnapshotGeometry` 显式区分「真实窗口原点」与「可见裁剪原点」。`capture_metadata.stage_timings` 报告窗口边界、WGC 尝试、回退激活、全屏捕获、解码、裁剪缩放和 capture 总耗时；WGC 成功时还包含 item、D3D、session setup、frame wait 和 texture readback 分段。

**UIA/MSAA 集成 + Gate** — 直接在 STA 线程内调用 COM（不再 per-property 提交到全局线程池——那会在 UIA provider 挂死时永久泄漏线程）。`UIAWalkGate` / `MSAAWalkGate` / `UIAActionGate`（挂在 `ServerContext`）提供窗口级超时边界：`wait_for + shield + done_callback`，超时时 gate 保持占用并标记不健康冷却，后续请求走 SOM、跳过 MSAA 或回退 pixel/HID，不再叠加 doomed calls。

**树质量 + SOM 回退** — `tree_quality: rich(>50) / ok(15-50)` 跳过 SOM，`poor(<15) / empty(0)` 启用 SOM 视觉检测。`uia_quality` 与 `vision_quality` 单独报告，不互相覆盖。

**PaddleOCR ONNX 后端** — OCR 默认使用 RapidOCR + ONNXRuntime，也就是 PaddleOCR PP-OCRv4 mobile ONNX 模型。`CUA_OCR_BACKEND=easyocr` 可启用可选 EasyOCR fallback（需安装 `ocr-easy` extra）。`CUA_OCR_TIMEOUT_SECONDS` 覆盖单次 OCR timeout；`CUA_RAPIDOCR_DEVICE=cpu` 是 v1 支持的运行方式。RapidOCR 首次初始化可能下载 ONNX 模型，离线/打包发布前需要确认这些模型已内置。OCR 文本结果会透传 `confidence` / `ocr_confidence` 到 vision nodes。默认去噪只做空文本过滤和确定性误读修正；删除孤立符号、微小 ASCII 和已知图标形状误读需显式设置 `CUA_OCR_DENOISE=strict`，`CUA_OCR_DENOISE=0` 可关闭修正。

**录制 vs 学习** — 录制是 agent 视角（MCP 工具调用回放），学习是人类视角（pynput + UIA 标注 + OCR 增强）。两者通过 `capture_service / ocr_service / uia_service / window_identity_service` 共享底层能力。

**录制写盘** — 录制 turn 文件夹通过专用 daemon writer 线程 + 有界队列写入，不与 anyio 默认 executor 竞争；队列满时丢弃并计数（`state.dropped_turns`）。

**学习 OCR 管道** — 录制中 UIA 读值，失败则截活动窗口；OCR 截图前用 `_is_sensitive_context` 二级判断（UIA `IsPassword` + 元素 title/identifier 模式匹配），密码或敏感字段跳过截图落盘。停止时 OCR 识别，全文数据存 `ocr_text`，不自动替换，留给模型分析。

**CDP target 跟踪** — `computer_page` 按稳定 `target_id`（CDP tab id）而非 `tab_index` 跟踪页面。`get_page_info` 返回 resolved `target_id`，调用方可用它把后续调用钉到同一 tab；失败重试时按 `target_id` 重解析，不会因 tab 顺序变化漂移到错误页面。

**回放窗口解析** — `_build_window_ref` 保存 `version / pid / window_id / origin / process_name / window_title / class_name / 尺寸`。回放时按以下顺序解析活窗口：(1) 原 HWND 仍有效 → 直接用；(2) 按 `process_name` 找首个可见窗口；(3) 按 `window_title` 子串匹配。click/drag/scroll 也会按 new_origin 重算坐标，不只有 `activate_window`。

### 已知限制

- **SOM 检测同步运行于事件循环线程**：anyio 任务组持有 GIL。warm_up() 启动时预加载（~30s），后续调用 < 3s
- **录制截图必须同步**：`ImageGrab.grab()` 不能卸到线程——anyio 事件循环阻止 `run_in_executor` 任务执行。50 步录制总计 ~5-10s 阻塞
- **SOM OCR 遮挡噪音**：WGC window capture 可避免大多数重叠窗口污染；screen crop 回退会在 `capture_metadata.occlusion_risk=true` 中显式标记
- **SOM YOLO/OCR 并行收益有限**：2026-06-01 EasyOCR CPU 基准（1280x830 WGC screenshot）串行 3.31s、`ThreadPoolExecutor` 并行 3.07s，仅约 8% 提升；RapidOCR 后端需重新基准后再决定是否并行
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

# WGC 受控场景 + 可选真实应用采样
uv run python scripts/wgc_smoke.py
uv run python scripts/wgc_smoke.py --live-only --app chrome --app dingtalk --repeat 3
```

---

## English

### Highlights

- **25 MCP tools** — screenshots, mouse, keyboard, window management; duplicate wrappers merged into higher-level entries
- **Recording / Replay** — record MCP tool invocations to disk with full replay (coordinate recalculation + stale-window resolution)
- **Learning mode** — record human actions (pynput global hooks + UIA element enrichment + PaddleOCR ONNX vision)
- **SOM vision detection** — OmniParser YOLO + PaddleOCR ONNX (Chinese + English) + TinyCLIP icon labels, automatic fallback when UIA is unavailable
- **UIA tree walking** — supports Win32 / UWP / Electron / Qt apps; auto-triggers SOM for non-UIA apps
- **Agent cursor overlay** — Dubins-path flight animation + spring overshoot + glow rendering

### Architecture

```
cua-pywin/
├─ computer_server/          ← Main package
│  ├─ mcp_server.py          ← 25 MCP tools (thin wrappers; logic in services/)
│  ├─ main.py                ← Local HTTP server (/status + /mcp + cua-win JSON RPC)
│  ├─ tool_cli.py            ← cua-win CLI / HTTP server launcher
│  ├─ server_context.py      ← Per-instance state (recording / learning /
│  │                          UIA walk gate / popover dedup)
│  ├─ handlers/              ← Platform primitives (1:1 OS API wrappers)
│  │  ├─ base.py             ← Abstract interfaces (ABCs)
│  │  ├─ factory.py          ← HandlerFactory + Handlers dataclass
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM tree walker (DFS, direct STA calls)
│  │  ├─ windows_wgc.py      ← Windows.Graphics.Capture target-window capture
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
│  ├─ ocr.py                 ← PaddleOCR ONNX / RapidOCR Chinese/English
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

The default OCR backend is RapidOCR + ONNXRuntime with PaddleOCR PP-OCRv4
mobile models. First initialization validates/downloads RapidOCR ONNX models;
subsequent daemon calls reuse loaded state.
OCR text nodes preserve `confidence`. Default denoising only drops empty text
and applies deterministic misread fixes; deletion-style filtering for isolated
symbols and single characters requires `CUA_OCR_DENOISE=strict`.

### Quick Start

```bash
# MCP stdio mode (Claude Code / Claude Desktop)
python -m computer_server.mcp_server

# Unified HTTP server (binds 127.0.0.1 by default; serves /status + /mcp + token JSON RPC)
cua-win serve --port 8000

# CLI daemon/client (auto-starts a local HTTP daemon by default; no manual serve needed)
cua-win doctor
cua-win status
cua-win list-tools
cua-win computer_get_screen_size
@'
{"window":{"id":5678,"pid":1234}}
'@ | cua-win call computer_get_window_state -
cua-win computer_press_key '{\"key\":\"Control_L+s\"}'
cua-win computer_mouse '{\"action\":\"move\",\"x\":100,\"y\":100}'
cua-win computer_recording '{\"action\":\"status\"}'
cua-win computer_learn '{\"action\":\"status\"}'
$batch = '[{"type":"wait","milliseconds":100},{"tool":"computer_get_screen_size","arguments":{}}]'
$batch | cua-win batch
cua-win stop

# Binding to a non-localhost host requires explicit consent — without it
# the server refuses to start.
cua-win serve --host 0.0.0.0 --port 8000 \
    --i-know-this-exposes-my-computer
```

The normal HTTP server exposes `/status` and `/mcp`. `cua-win` daemon mode
reuses the same app and additionally enables token-protected `/tools`, `/call`,
and `/shutdown` JSON RPC routes. CORS only allows
`localhost / 127.0.0.1 / [::1]` origins.

`cua-win` is the human/script-facing CLI. It starts the same local HTTP app by
default and calls MCP tools through token-protected JSON RPC routes, so heavy
high-frequency tools such as `computer_get_window_state` pay warm-up once per
daemon process. `--no-daemon` forces a one-shot in-process call. `list-tools`,
`describe`, and explicitly safe read-only status calls use a lightweight import
path that skips SOM/TinyCLIP/RapidOCR warm-up. Desktop-control and vision calls
should still reuse the daemon.
`cua-win <tool> '<json>'` is rewritten to `cua-win call <tool> '<json>'`.
For Chinese text or deeply nested arguments, prefer UTF-8 stdin
(`'<json>' | cua-win call <tool> -`) or a UTF-8 file
(`cua-win call <tool> '@arguments.json'`) to avoid PowerShell/native argument
escaping and console code-page transformations. Inline JSON remains supported.

`cua-win batch` reads an action array or a
`{"defaults":...,"actions":[...]}` object from stdin, inline JSON, or a UTF-8
JSON file and executes it sequentially through the same daemon process. Entries
may be `{"tool":"computer_click","arguments":{...}}` or
`{"type":"wait","milliseconds":200}`. Execution stops on the first failure by
default; `--continue-on-error` processes the remaining entries. Failures and
Ctrl+C interruptions trigger a best-effort `computer_release_all_inputs` call.
Batch calls suppress the agent cursor trajectory overlay so dense action
sequences do not wait on visual animation; the actual drag `duration` remains
unchanged.

`computer_launch_app` reports process launch completion by default. Pass
`{"wait_for_window":true,"wait_timeout":10}` when the next step needs a
targetable top-level window and canonical `window`. Apps such as WeChat may
reuse an existing process or expose their main HWND from a process other than
the launcher PID, so follow-up calls should use the returned
`window.pid/window.id`. A wait timeout preserves the successful launch result
and explicitly returns `window_ready=false`.

For repeated operations, `defaults` supplies common arguments per tool. The
merge is shallow and each action's arguments take precedence:

```json
{
  "defaults": {
    "computer_drag": {
      "window": {"id": 123456, "pid": 8888},
      "screenshot_id": "shot-123",
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

`cua-win doctor` performs read-only local diagnostics for platform, Python
version, dependency availability, bundled model files, state directory, and
existing daemon reachability. It does not start the daemon or call desktop
control tools.

Use `cua-win serve --no-cli-rpc` when you only want `/status + /mcp`.

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

**Screen & Mouse** — `computer_screenshot` `computer_click` `computer_perform_secondary_action` `computer_mouse` `computer_drag` `computer_scroll` `computer_release_all_inputs` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**Keyboard** — `computer_type` `computer_press_key` `computer_set_value`

**Windows & Apps** — `computer_open_url` `computer_launch_app` `computer_get_window` `computer_manage_window` `computer_list_windows` `computer_list_apps`

**Desktop & Browser** — `computer_get_desktop_environment` `computer_page`

**Recording & Replay** — `computer_recording` `computer_replay_trajectory`

**Learning** — `computer_learn`

### Coordinate Systems

| Space | Description |
|---|---|
| Screen coords | Absolute screen position, used by `WindowFromPoint` / `GetWindowRect` |
| Full-screen pixels | `screenshot()` output, full virtual desktop |
| Window pixels (full-res) | After cropping full-screen shot to window rect |
| Window pixels (resized) | After `max_image_dimension` downscale in `get_window_state` |
| Zoom pixels | Full-resolution region cropped by `computer_zoom` |

Click coordinate pipeline: `element_index → cached element center → +window origin → screen coords → scale → actual click`.

### Window Contract

Prefer passing returned `window` objects across discovery, observation, and
action tools:

```json
{"window":{"id":5678,"pid":1234}}
```

`window.id` and `window.window_id` both refer to the HWND. Top-level
`pid/window_id` remains supported, but when both forms are provided they must
match or the tool returns a validation error.
`computer_list_windows`, `computer_list_apps`, `computer_get_window`, and
`computer_get_window_state` return reusable `window` objects. Use
`computer_get_window({"id":...,"app":...})` to rehydrate a current window after
stale handles, lost bindings, or context resets. Use
`computer_get_window({"active":true})` for the current foreground window.
`window_ref` is internal recording/replay identity metadata and should not be
hand-authored.
`computer_type` and `computer_press_key` also accept canonical `window`.
Targeted text, hotkey, key-press, and key-down calls activate and verify that
window before sending input. Calls without `window/window_id` preserve
current-focus behavior. Key-up skips activation so globally held input can
always be released.
`computer_click`, `computer_drag`, and `computer_scroll` also activate and
verify the target when canonical `window` is explicitly supplied. Activation
failure prevents mouse input. Legacy top-level `pid/window_id` calls preserve
their prior foreground behavior.

Merged entry points: hotkeys use `computer_press_key({"keys":[...]})` or
`computer_press_key({"key":"Control_L+s"})`; activate/minimize/maximize/close,
move, and resize use `computer_manage_window`; app/title/query window search
uses `computer_list_windows`; mouse move/down/up primitives use
`computer_mouse({"action":"move|down|up"})`; keyboard down/up primitives use
`computer_press_key({"key":"Shift","action":"down|up"})`; recording
start/stop/status uses `computer_recording({"action":"start|stop|status"})`;
learning start/stop/status uses `computer_learn({"action":"start|stop|status"})`;
accessibility-tree reads use `computer_get_window_state` `tree_markdown` and
`nodes`.
Use `computer_click` for a node's primary action and
`computer_perform_secondary_action` for an explicitly exposed non-primary
action. The current secondary action is `action="expand"` for MSAA
dropdown/splitbutton nodes. Window activation remains
`computer_manage_window({"action":"activate",...})`.

Prefer `screenshot_id` for coordinate chains. `computer_get_window_state`
returns both `snapshot_id` and `screenshot_id`: `snapshot_id` binds the
observation/node snapshot, while `screenshot_id` binds that capture's crop
origin, resize ratio, and backend. Pass `screenshot_id` to
`computer_click`, `computer_drag`, `computer_scroll`, or `computer_zoom` when
coordinates come from a returned screenshot, so later observations cannot
overwrite the geometry used for coordinate conversion.
Returned `nodes` use `nodes_coordinate_space="window_resized"`, so their
bounding boxes and centers align directly with the target screenshot.
`computer_get_window_state` also returns a `screenshots` array. The first item
is the target (`role="target", zIndex=0`); visible related menus, popovers, and
dialogs are returned as `role="transient"` items. Each item has its own
`window`, `screenshot_id`, screen origin, and `zIndex` for follow-up coordinate
actions. The existing top-level `screenshot` and `screenshot_id` remain the
primary target capture.

### Key Design Decisions

**Per-window screenshot capture** — `PrintWindow` fails on hardware-accelerated windows. `get_window_state` now prefers Windows.Graphics.Capture for the target window's own DWM-composited frame, avoiding overlapping foreground-window pollution. If WGC fails, it falls back to `ImageGrab.grab()` + `GetWindowRect` + PIL `crop()`. `computer_zoom` follows the last snapshot backend: WGC snapshots use WGC for native zoom crops and fall back to screen crop only when WGC fails. Multi-monitor negative coordinates and partially-offscreen windows are handled by `WindowSnapshotGeometry`, which explicitly separates the real window origin from the visible crop origin. `capture_metadata.stage_timings` reports window-bounds, WGC attempt, fallback activation, screen capture, decode, crop/resize, and total capture latency. Successful WGC captures additionally expose item, D3D, session setup, frame wait, and texture readback timings.

**UIA/MSAA integration + gates** — COM is called directly inside its STA thread (the previous design submitted each property call to a shared executor and leaked threads when the UIA provider hung). `UIAWalkGate` / `MSAAWalkGate` / `UIAActionGate` (owned by `ServerContext`) provide per-window timeout boundaries with `wait_for + shield + done_callback`. On timeout the gate stays held and the key is marked unhealthy, so fresh requests fall through to SOM, skip MSAA, or use pixel/HID fallback instead of stacking more doomed calls.

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

**PaddleOCR ONNX backend** — OCR defaults to RapidOCR + ONNXRuntime with
PaddleOCR PP-OCRv4 mobile models. `CUA_OCR_BACKEND=easyocr` enables the optional
EasyOCR fallback when installed with the `ocr-easy` extra. `CUA_OCR_TIMEOUT_SECONDS`
overrides per-call OCR timeout, and `CUA_RAPIDOCR_DEVICE=cpu` is the supported v1
runtime. RapidOCR may download ONNX models on first initialization; package/offline
builds should verify these model files are vendored before release. OCR text results
propagate `confidence` / `ocr_confidence` into vision nodes. Default denoising only
drops empty text and applies deterministic misread fixes; deletion-style filtering
for isolated symbols, tiny ASCII artifacts, and known icon-shape misreads requires
`CUA_OCR_DENOISE=strict`. Set `CUA_OCR_DENOISE=0` to disable denoising.

**Recording vs Learning** — Recording is agent-perspective (MCP tool replay); Learning is human-perspective (pynput + UIA + OCR enrichment). The two share capture / OCR / UIA-timeout / window-identity through `services/` rather than duplicating logic.

**Recording disk writes** — Turn-folder writes go through a dedicated daemon writer thread + bounded queue so they don't compete with the anyio default executor; full queue drops with a counter in `state.dropped_turns`.

**Learning OCR pipeline** — UIA value probe at record time; active-window screenshot on failure. Before any OCR screenshot lands on disk, `_is_sensitive_context` gates it (UIA `IsPassword` + element title/identifier pattern match) so password / sensitive fields are skipped. At stop, OCR produces full `ocr_text` with bbox+confidence. Model decides, no auto-replacement.

**CDP target tracking** — `computer_page` tracks pages by stable `target_id` (CDP tab id) rather than `tab_index`. `get_page_info` returns the resolved `target_id` so callers can pin subsequent calls to the same tab; retries re-resolve by `target_id`, never drifting to the wrong page when tab order changes.

**Replay window resolution** — `_build_window_ref` stores `version / pid / window_id / origin / process_name / window_title / class_name / size`. At replay time the live window is resolved in this order: (1) original HWND if still valid, (2) first visible window matching `process_name`, (3) substring match on `window_title`. click / drag / scroll also rewrite coordinates against the new origin — not just `activate_window`.

### Known Limitations

- **SOM detection runs synchronously on the event loop**: anyio task group holds the GIL. warm_up() pre-loads at startup (~30s); subsequent calls <3s
- **Recording screenshots cannot be offloaded**: `ImageGrab.grab()` must stay synchronous — anyio blocks `run_in_executor`. ~5-10s total for 50-action recordings
- **SOM OCR occlusion noise**: WGC window capture avoids most overlapping-window pollution; screen-crop fallback marks `capture_metadata.occlusion_risk=true`
- **Limited SOM YOLO/OCR parallelism gain**: the 2026-06-01 EasyOCR CPU benchmark on a 1280x830 WGC screenshot measured 3.31s serial vs 3.07s with `ThreadPoolExecutor` parallelism, about 8% faster; re-benchmark with RapidOCR before enabling parallelism
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

# Controlled WGC scenarios and optional live-app samples
uv run python scripts/wgc_smoke.py
uv run python scripts/wgc_smoke.py --live-only --app chrome --app dingtalk --repeat 3
```
