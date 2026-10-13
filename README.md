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
- **SOM 视觉检测** — OmniParser V2 YOLO 图标定位 + PaddleOCR ONNX 中文识别，主 LLM 结合完整截图理解界面，UIA 不可用时自动回退
- **UIA 树遍历** — 支持 Win32 / UWP / Electron / Qt 应用，非 UIA 应用自动触发 SOM
- **Agent 光标覆盖层** — Dubins 路径飞行动画 + 弹簧过冲 + 辉光渲染

### 架构

```
cua-pywin/
├─ computer_server/          ← 主程序
│  ├─ mcp_server.py          ← 25 MCP 工具入口（薄 wrapper，业务转交 services/）
│  ├─ main.py                ← 本机 HTTP 服务器（/status + /mcp + cua-win JSON RPC）
│  ├─ tool_cli.py            ← cua-win CLI / HTTP server launcher
│  ├─ tool_rpc.py            ← 共享 tool schema / result JSON 序列化
│  ├─ server_context.py      ← 每实例状态（录制/学习/observation registry/UIA gate）
│  ├─ handlers/              ← 平台原语（OS API 一对一封装）
│  │  ├─ base.py             ← 抽象接口 (ABCs)
│  │  ├─ factory.py          ← HandlerFactory + Handlers dataclass
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM 树遍历 (DFS, 缓存属性, 直接 STA 调用)
│  │  ├─ windows_msaa.py     ← MSAA / IAccessible 经典控件回退
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
│  │  ├─ protocol_service.py       ← 0.2 envelope / error / artifact / action
│  │  ├─ observation_context.py    ← observation/screenshot/zoom freshness
│  │  ├─ action_delta_service.py   ← 可选动作前后差异摘要
│  │  ├─ agent_state_service.py    ← agent 紧凑观察文本
│  │  ├─ coordinate_service.py     ← 坐标/zoom/元素中心解析（纯函数）
│  │  ├─ window_state_service.py   ← WindowSnapshotGeometry + UIAWalkGate +
│  │  │                              get_window_state_snapshot + create_zoom_image
│  │  ├─ window_identity_service.py ← build/resolve_live_window（3 级 fallback）
│  │  ├─ replay_resolver.py        ← 回放时坐标重算 + 过期 HWND 重写
│  │  ├─ capture_service.py        ← virtual desktop 截图（录制 + 学习共享）
│  │  ├─ ocr_service.py            ← lazy OCRProcessor 单例
│  │  ├─ target_profile_service.py ← 窗口类型分类 + 投递风险
│  │  ├─ permission_service.py     ← open/command/CDP 权限策略
│  │  ├─ validation_service.py     ← 参数与路径校验
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
│  ├─ wgc_smoke.py           ← WGC 遮挡/最小化/显示器 DPI/真实应用 smoke
│  ├─ agent_smoke_fixture.py ← 固定 Windows GUI fixture
│  └─ run_agent_smoke.py     ← agent smoke prompt / transcript checker
├─ pyproject.toml
└─ README.md
```

**层依赖方向**：`mcp_server → services → handlers → OS API`。`handlers` 不依赖 `services`；`recording / learning / overlay` 通过 `services` 复用截图、OCR、UIA timeout 能力，不重复实现。

### 安装

正式发布包从 [GitHub Releases](https://github.com/zhangweiooy/cua-pywin/releases)
下载。普通 Windows 用户优先使用安装器：

```powershell
.\cua-win-0.4.0-win64-setup.exe
cua-win doctor
```

安装器会把完整 standalone 运行目录安装到
`%LocalAppData%\Programs\cua-win`，不需要管理员权限；安装时可选把
`cua-win.exe` 加入当前用户 PATH。模型随安装器一起交付，用户不需要单独下载。

wheel 也内置 OmniParser V2 YOLO 和 RapidOCR 模型，适合 Python
环境或开发验证：

```powershell
pip install .\cua_pywin-0.4.0-py3-none-any.whl
cua-win doctor
```

wheel 约 660 MiB，因此不通过常规 PyPI 分发。wheel 安装过程仍可能从包索引下载
Python 依赖；完整的开发者发布流程见 [`RELEASE.md`](RELEASE.md)。

源码开发安装：

```bash
cd D:\Documents\Code\cua-pywin
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

uv：

```bash
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

默认 OCR 后端是 RapidOCR + ONNXRuntime（PaddleOCR PP-OCRv4 mobile 模型）。
det/cls/rec ONNX 模型和中文字符字典已内置到项目，首次初始化不需要联网；
后续 daemon 调用复用已加载状态。
OCR 文本节点会保留 `confidence`。默认去噪只做空文本过滤和确定性误读修正；
删除孤立符号/单字符的严格过滤需显式设置 `CUA_OCR_DENOISE=strict`。

### 快速开始

```bash
# MCP stdio 模式（Claude Code / Claude Desktop）
cua-win mcp

# 输出 copy-paste 安全的 MCP 客户端配置 / 安装命令
cua-win mcp-config
cua-win mcp-config --client claude
cua-win mcp-config --client codex

# 统一 HTTP 服务（默认只监听 127.0.0.1，提供 /status + /mcp + token JSON RPC）
cua-win serve --port 8000

# CLI daemon/client（默认自动启动本机 HTTP daemon，无需手动 serve）
cua-win doctor
cua-win status
cua-win list-tools
cua-win computer_get_screen_size
@'
{"window":{"id":5678,"pid":1234},"format":"png","max_image_dimension":1280}
'@ | cua-win call computer_screenshot - --output window.png
@'
{"window":{"id":5678,"pid":1234}}
'@ | cua-win call computer_get_window_state -
cua-win computer_press_key '{\"key\":\"Control_L+s\"}'
cua-win computer_mouse '{\"action\":\"move\",\"x\":100,\"y\":100}'
cua-win computer_recording '{action:status}'
cua-win computer_learn '{action:status}'
$batch = '[{"type":"wait","milliseconds":100},{"tool":"computer_get_screen_size","arguments":{}}]'
$batch | cua-win batch
cua-win stop

# 若要绑定到非 localhost，必须显式确认（否则启动会被拒绝）
cua-win serve --host 0.0.0.0 --port 8000 \
    --i-know-this-exposes-my-computer
```

普通 HTTP 服务暴露 `/status` 与 `/mcp`；`cua-win` daemon 复用同一个 app，并额外启用 token 保护的 `/tools`、`/call`、`/shutdown` JSON RPC 路由。CORS 仅放行 `localhost / 127.0.0.1 / [::1]` origin。

`cua-win` 是面向人和脚本的 CLI。默认启动同一个本机 HTTP app，并通过 token 保护的 JSON RPC 路由调用 MCP 工具，因此 `computer_get_window_state` 这类高频重工具只在 daemon 启动时预热一次。`--no-daemon` 会强制单次进程内调用；`list-tools`、`describe` 和明确安全的只读状态调用使用轻量导入，不会启动 SOM/RapidOCR 预热。桌面控制和视觉调用仍推荐复用 daemon。
`cua-win <tool> '<json>'` 会自动等价转换为 `cua-win call <tool> '<json>'`。
包含中文或复杂嵌套参数时，推荐通过 UTF-8 stdin
（`'<json>' | cua-win call <tool> -`）或 UTF-8 文件
（`cua-win call <tool> @arguments.json`）传入，避免 PowerShell/native
参数转义和控制台代码页改变内容。PowerShell 管道中文前需确保
`$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`，否则文本可能在
进入 `cua-win` 前就被替换。内联 JSON 仍保持兼容；扁平 primitive 参数
也支持窄范围简写，例如 `{action:status}`。

`cua-win batch` 从标准输入、内联 JSON 或 UTF-8 JSON 文件读取动作数组，或
`{"defaults":...,"actions":[...]}` 对象，并在同一 CLI 进程中通过常驻 daemon
顺序执行。条目可以是
`{"tool":"computer_click","arguments":{...}}`，也可以是
`{"type":"wait","milliseconds":200}`。默认首个失败即停止；使用
`--continue-on-error` 可继续后续条目。失败或 Ctrl+C 中断时会尝试调用
`computer_release_all_inputs`。批处理调用会关闭 agent 鼠标轨迹动画，避免大量连续
动作在 overlay 动画上产生额外等待；真实鼠标拖拽的 `duration` 仍按动作参数执行。
使用 `cua-win batch --dry-run` 可以只校验并汇总展开后的动作，不启动 daemon，
也不会发送输入。

支持 `capture_after` 的动作会在 `data.capture_after` 返回后置窗口观察。需要轻量前后对比时，再额外传
`include_post_action_delta=true`；这会增加一次动作前窗口观察，并返回
`data.post_action_delta`，汇总节点数量变化、新增/移除节点、文本变化、tree quality
变化以及前后 `screenshot_id`。

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

`cua-win doctor` 执行只读本机诊断：平台、Python 版本、依赖可发现性、打包模型文件、状态目录、interactive desktop、窗口枚举、全屏截图可用性和现有 daemon 可达性。它不会启动 daemon 或执行输入动作；可用 `--no-desktop` 跳过桌面探测。
真实应用诊断可运行 `uv run python scripts/windows_robustness_smoke.py`；脚本会对
Notepad、Edge/Chrome、Windows Terminal、微信和钉钉输出只读 JSON 摘要，不打印
截图或节点文本。

如果只需要 `/status + /mcp`，可用 `cua-win serve --no-cli-rpc` 关闭 CLI JSON RPC 路由。

### Claude Code 集成

推荐直接生成配置或安装命令，避免手写 PowerShell JSON：

```powershell
cua-win mcp-config
cua-win mcp-config --client claude
```

`.mcp.json` 等价形态：

```json
{
  "mcpServers": {
    "cua-win": {
      "command": "cua-win",
      "args": ["mcp"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### Codex Skill

本仓库内置 Codex skill：`skills/cua-win`。把该目录复制或链接到
`$CODEX_HOME/skills/cua-win`（未设置 `CODEX_HOME` 时为
`%USERPROFILE%\.codex\skills\cua-win`）后，可在 Codex 中用
`$cua-win` 触发这套 Windows 自动化工作流说明。

### MCP 工具

**屏幕与鼠标** — `computer_screenshot` `computer_click` `computer_perform_secondary_action` `computer_mouse` `computer_drag` `computer_scroll` `computer_release_all_inputs` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**键盘** — `computer_type` `computer_press_key` `computer_set_value`

**窗口与应用** — `computer_open_url` `computer_launch_app` `computer_get_window` `computer_manage_window` `computer_list_windows` `computer_list_apps`

**桌面与浏览器** — `computer_get_desktop_environment` `computer_page`

**录制回放** — `computer_recording` `computer_replay_trajectory`

**学习** — `computer_learn`

### Protocol 0.2

所有 25 个工具都返回固定 envelope；成功和可预期业务失败使用相同结构：

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

工具专属结果只放在 `data`。图片只存在于 MCP image content，并由
`artifacts[].content_index` 关联；结构化结果不再包含 base64 或调试输出路径。
副作用调用返回临时 `action_id` 以及
`completed/rejected/unknown` outcome。`unknown`、stale binding 和后置条件失败都
要求重新观察，不应盲目重复输入。参数错误、目标拒绝和 provider 不可用属于正常
MCP result，使用 `success=false` 和稳定的 `error.code`；未捕获异常或传输故障才使用
MCP `isError`。

daemon `/call` 返回
`{protocol_version,result,content,transport_error}`。CLI 普通输出和 `--json`
都直接打印 `result` envelope；使用 `cua-win call TOOL ... --output PATH`
保存主图，或使用 `--output-dir DIR` 保存全部图片 artifact。

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

Protocol 0.2 的公共窗口对象严格为 `{"id": HWND, "pid": PID}`，不接受
`window_id`、`windowId`、额外窗口元数据或顶层目标 `pid/window_id`。
`computer_list_windows`、`computer_list_apps`、
`computer_get_window` 和 `computer_get_window_state` 都会返回可直接复用的
`window` 对象。使用 `computer_get_window({"id":...,"app":...})` 可以在 stale
handle、绑定丢失或上下文重置后重新获取当前窗口对象；使用
`computer_get_window({"active":true})` 获取当前前台窗口。
`window_ref` 仅用于 recording/replay 的内部窗口身份快照，不要求调用方构造。
`computer_type` 和 `computer_press_key` 也支持 canonical `window`。传入目标
窗口时，会先激活并验证前台窗口，再发送文本、热键、按键或 key-down；不传
`window` 时继续作用于当前焦点。key-up 为保证能释放全局按键状态，
不会重新激活目标窗口。
`computer_click`、`computer_drag` 和 `computer_scroll` 在显式传入 `window`
时也会先激活并验证目标窗口；激活失败时不会发送鼠标输入。
动作结果的 `data.action_path` 会报告 `target_profile`、`delivery_risk` 和
`delivery_note`，用于解释当前目标是否类似 Chromium、终端、WPF/WinUI、
VCL/SAL 或通用窗口；这些字段只描述风险，不改变工具默认行为。

合并后的入口约定：热键通过 `computer_press_key({"keys":[...]})` 或
`computer_press_key({"key":"Control_L+s"})` 执行；窗口激活、最小化、最大化、
关闭、移动和缩放统一走 `computer_manage_window`；按 app/title/query 查窗口统一走
`computer_list_windows`；鼠标 move/down/up 低层动作统一走
`computer_mouse({"action":"move|down|up"})`；键盘 down/up 低层动作统一走
`computer_press_key({"key":"Shift","action":"down|up"})`；录制 start/stop/status
统一走 `computer_recording({"action":"start|stop|status"})`；学习 start/stop/status
统一走 `computer_learn({"action":"start|stop|status"})`；辅助树读取统一走
`computer_get_window_state` 的 `tree_markdown` 和 `nodes`。

Teaching start/stop 必须绑定同一个非空 `session_id`。status 可通过
`after_sequence` 和 `timeline_limit`（1–200，默认 64）增量读取严格动作时间线；
status 返回的游标字段为 `data.next_sequence`。stop 返回
`data.sessionId`、`data.cutoffSequence`、严格 `data.actions[]`，以及只包含
`durationSeconds`、`actionCount`、`observationCount` 和
`degradedObservationCount` 的低成本 `data.summary`。重复或并发 stop 必须携带
原 session id，并返回绑定到该 session 的同一完成结果。`computer_learn` 不接受
`excluded_window_ids`。

```powershell
$sessionId = "teaching-123"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@"
{"action":"start","session_id":"$sessionId","task_description":"演示导出文件"}
"@ | cua-win call computer_learn -

cua-win computer_learn '{"action":"status","after_sequence":0,"timeline_limit":64}'

@"
{"action":"stop","session_id":"$sessionId"}
"@ | cua-win call computer_learn -
```

使用 `cua-win help computer_learn` 或 `cua-win computer_learn --help` 查看
工具专属参数；使用 `cua-win describe computer_learn --no-daemon` 获取机器可读
schema。

双击使用 `computer_click({"count":2,...})`，没有单独的 double-click 工具。
录制 start 的 `output_dir` 必须是新目录或空目录；cua-win 不会覆盖已有
recording，也不会把旧 turn 混入新的 0.2 trajectory。
节点的主动作使用 `computer_click`；节点明确暴露的非主动作使用
`computer_perform_secondary_action`。当前仅支持 MSAA dropdown/splitbutton 的
`action="expand"`。窗口置前不属于 accessibility action，统一使用
`computer_manage_window({"action":"activate",...})`。Windows 激活会恢复已最小化
的目标并验证其成为前台窗口；该路径临时连接相关输入线程，不注入模拟 Alt
按键。激活失败时，显式指定窗口的输入工具不会继续发送输入。

`computer_get_window_state` 要求 canonical `window`，并返回
`data.observation_id`、`data.screenshot_id` 和对应图片 artifact。
`element_index` 动作必须同时传入同一窗口最新的 `observation_id`；
`window_resized/window_full_res` 坐标必须传入最新 `screenshot_id`；
`computer_zoom` 也必须引用最新 `screenshot_id`，其返回的 `zoom_id` 是后续 zoom
坐标动作的唯一绑定。同一窗口产生新 observation 后，旧 observation、
screenshot 和 zoom 立即 stale。直接 screen 坐标、无目标低级 mouse、当前焦点
type/key，以及明确 canonical window 的 type/key/manage 不要求 observation。
返回的 `nodes` 使用 `nodes_coordinate_space="window_resized"`，其 `bbox`
和中心坐标可直接与主截图对齐。默认只返回 100 个最高优先级节点，避免
节点过多的应用一次性撑大 payload；可传 `max_nodes=0` 返回全部节点，或配合
`query` / `max_nodes` 先筛选再调整上限。该限制只影响返回 payload，不影响内部
`ElementCache`，后者仍保留完整窗口全分辨率坐标。
`tree_markdown` 默认不返回；仅在调试完整树时传
`include_tree_markdown=true`。`response_mode="compact"` 会把图片最长边限制为
960、节点限制为 30、普通摘要限制为 300 字，并省略详细 capture/provider/vision
诊断。公开响应中的浮点数统一保留 2 位小数，内部 observation 和坐标换算仍保留
原始精度。
节点同时包含底层 `actions` 和面向 agent 的 `action_hints`（如 `click`、`type`、
`set_value`、`expand`），便于区分可点击、可输入和可展开候选。
`data.accessibility_hint` / `data.observation_recommendation` 会说明语义树是否稀疏、
是否已启用视觉/OCR，以及是否应继续用 `query` 或提高 `max_nodes` 收窄观察。
`data.state_text` 是默认给 agent 读取的唯一紧凑状态文本；
`data.agent_state` 只保留截断和采样元数据，不再重复保存文本。摘要默认由
`agent_text_limit=500` 限制长度；只有摘要不足时才使用
`show_full_text=true` 或提高 `agent_text_limit`，再进一步检查 `nodes`；需要
完整调试树时显式启用 `include_tree_markdown=true`。
公开 payload 只保留 canonical 节点计数；严格相等的重复计数和空诊断字段会被省略。
目标与 transient capture 的结构化描述只存在于 envelope 的 `artifacts[]`；
实际图片位于 MCP image content，对应 artifact 会给出 role、MIME、尺寸、字节数、
`content_index`、`window`、`observation_id` 和 `screenshot_id`。

### 关键设计

**Per-window 截图** — Windows `PrintWindow` 对硬件加速窗口失效。`computer_screenshot({"window":{"id":...,"pid":...}})` 和 `get_window_state` 共享 Windows.Graphics.Capture 优先、`ImageGrab.grab()` + `GetWindowRect` + PIL `crop()` 回退的像素捕获链路。前者只返回窗口图片，不运行 UIA/MSAA/SOM，也不创建 `observation_id` / `screenshot_id`；后续需要节点或窗口相对坐标绑定时必须使用 `get_window_state`。`computer_zoom` 会沿用上一次 observation 的 capture backend：WGC capture 使用 WGC 做原生 zoom crop，失败才退回 screen crop。多显示器负坐标 + 部分屏外通过 `WindowSnapshotGeometry` 显式区分「真实窗口原点」与「可见裁剪原点」。`capture.capture_metadata.stage_timings` 报告窗口边界、WGC 尝试、回退激活、全屏捕获、解码、裁剪缩放和 capture 总耗时；WGC 成功时还包含 item、D3D、session setup、frame wait 和 texture readback 分段。`capture.capture_metadata.capture_reliability`、`degraded_reason` 和 `provider_errors` 会说明 WGC 回退、最小化窗口、screen-crop 遮挡风险或 provider 失败。

**UIA/MSAA 集成 + Gate** — 直接在 STA 线程内调用 COM（不再 per-property 提交到全局线程池——那会在 UIA provider 挂死时永久泄漏线程）。`UIAWalkGate` / `MSAAWalkGate` / `UIAActionGate`（挂在 `ServerContext`）提供窗口级超时边界：`wait_for + shield + done_callback`，超时时 gate 保持占用并标记不健康冷却，后续请求走 SOM、跳过 MSAA 或回退 pixel/HID，不再叠加 doomed calls。

**树质量 + SOM 回退** — `tree_quality: rich(>50) / ok(16-50)` 跳过 SOM，`poor(1-15) / empty(0)` 启用 SOM 视觉检测。`uia_quality` 与 `vision_quality` 单独报告，不互相覆盖。

**完整截图 + 节点定位** — 主 LLM 结合完整窗口截图、UIA/MSAA 语义、OCR 文本和
YOLO 图标边界框理解界面。优先用已有 `element_index` 和最新 `observation_id`
操作小控件；YOLO 节点的 `name` 为空，检测置信度不代表图标含义的置信度。
目标仍不明确时，用 `computer_zoom` 和父 `screenshot_id` 局部放大；只有需要在
放大图中定位时才使用 `zoom_id` 坐标。查询文本不会匹配未命名图标，必要时取消
`query` 或增加 `max_nodes` 查看候选。运行时只使用本地 YOLO/OCR，不加载 Florence，
也不逐图标调用 LLM API。SOM 在语义树不足或显式 `force_som=true` 时运行。

现行运行方案、模型来源和验证范围见[视觉运行方案与 YOLO ONNX 验证](docs/yolo-onnx-poc.md)。

**PaddleOCR ONNX 后端** — OCR 使用 RapidOCR + ONNXRuntime，也就是 PaddleOCR PP-OCRv4 mobile ONNX 模型。det/cls/rec 模型和中文字符字典固定内置在 `computer_server/models/rapidocr_ppocrv4/`，运行时显式传入本地路径，不支持模型下载或自动更新。模型升级只随开发者发布的新版本交付。`cua-win doctor` 校验文件 SHA-256，并拒绝未拉取的 Git LFS 指针。`CUA_RAPIDOCR_MODEL_DIR` 仅用于开发者本地验证。`CUA_OCR_TIMEOUT_SECONDS` 覆盖单次 OCR timeout；`CUA_RAPIDOCR_DEVICE=cpu` 是 v1 支持的运行方式。OCR 文本结果会透传 `confidence` / `ocr_confidence` 到 vision nodes。默认去噪只做空文本过滤和确定性误读修正；删除孤立符号、微小 ASCII 和已知图标形状误读需显式设置 `CUA_OCR_DENOISE=strict`，`CUA_OCR_DENOISE=0` 可关闭修正。

**录制 vs 学习** — 录制是 agent 视角（MCP 工具调用回放），学习是人类视角（pynput + UIA 标注 + OCR 增强）。两者通过 `capture_service / ocr_service / uia_service / window_identity_service` 共享底层能力。

**录制写盘** — 录制 turn 文件夹通过专用 daemon writer 线程 + 有界队列写入，不与 anyio 默认 executor 竞争；队列满时丢弃并计数（`state.dropped_turns`）。

**学习 OCR 管道** — 录制中 UIA 读值，失败则截活动窗口；OCR 截图前用 `_is_sensitive_context` 二级判断（UIA `IsPassword` + 元素 title/identifier 模式匹配），密码或敏感字段跳过截图落盘。停止时 OCR 识别，全文数据存 `ocr_text`，不自动替换，留给模型分析。短会话语义时间线会在 `observations[]` 和 `metrics` 中附加 `target_profile`、`capture_reliability`、`semantic_degraded_reason` 和 `delivery_risk`。

**CDP target 跟踪** — `computer_page` 按稳定 `target_id`（CDP tab id）而非 `tab_index` 跟踪页面。`get_page_info` 返回 resolved `target_id`，调用方可用它把后续调用钉到同一 tab；失败重试时按 `target_id` 重解析，不会因 tab 顺序变化漂移到错误页面。

**回放窗口解析** — `_build_window_ref` 保存 `version / pid / window_id / origin / process_name / window_title / class_name / 尺寸`。回放时按以下顺序解析活窗口：(1) 原 HWND 仍有效 → 直接用；(2) 按 `process_name` 找首个可见窗口；(3) 按 `window_title` 子串匹配。click/drag/scroll 也会按 new_origin 重算坐标，不只有 `activate_window`。

### 已知限制

- **SOM 检测仍有每帧成本**：同步模型推理由工作线程执行，warm-up 只预加载模型；首帧和后续检测仍增加 `computer_get_window_state` 的延迟
- **录制截图仍有动作内成本**：turn 写盘已进入有界 writer 线程，但每个被采样动作的截图捕获通常仍增加约 50-200 ms
- **SOM OCR 遮挡噪音**：WGC window capture 可避免大多数重叠窗口污染；screen crop 回退会在 `capture.capture_metadata.occlusion_risk=true` 中显式标记
- **学习 IME 对终端无效**：PowerShell/CMD 无 UIA ValuePattern，回退拼音 + OCR
- **录制坐标绝对化**：回放时窗口位置变化导致错位。`window_ref` 机制在窗口存活时可重算坐标，窗口过期则按进程名/标题重新搜索
- **GBK 编码**：子进程必须设置 `PYTHONIOENCODING=utf-8`

### 开发

```bash
# 单工具测试
uv run python scripts/mcp_test_one.py <tool_name> [json_args|-|@file] --timeout 120
uv run python scripts/mcp_test_one.py --list

# 编译检查
python -m py_compile computer_server/*.py
python -m py_compile computer_server/handlers/*.py
python -m py_compile computer_server/recording/*.py
python -m py_compile computer_server/learning/*.py

# WGC 受控场景 + 可选真实应用采样
uv run python scripts/wgc_smoke.py
uv run python scripts/wgc_smoke.py --live-only --app chrome --app dingtalk --repeat 3

# Agent smoke 骨架
uv run python scripts/agent_smoke_fixture.py
uv run python scripts/run_agent_smoke.py --dry-run --scenario fixture-basic
```

---

## English

### Highlights

- **25 MCP tools** — screenshots, mouse, keyboard, window management; duplicate wrappers merged into higher-level entries
- **Recording / Replay** — record MCP tool invocations to disk with full replay (coordinate recalculation + stale-window resolution)
- **Learning mode** — record human actions (pynput global hooks + UIA element enrichment + PaddleOCR ONNX vision)
- **SOM vision detection** — OmniParser V2 YOLO icon locations + PaddleOCR ONNX (Chinese + English), interpreted by the main LLM with the full screenshot, automatic fallback when UIA is unavailable
- **UIA tree walking** — supports Win32 / UWP / Electron / Qt apps; auto-triggers SOM for non-UIA apps
- **Agent cursor overlay** — Dubins-path flight animation + spring overshoot + glow rendering

### Architecture

```
cua-pywin/
├─ computer_server/          ← Main package
│  ├─ mcp_server.py          ← 25 MCP tools (thin wrappers; logic in services/)
│  ├─ main.py                ← Local HTTP server (/status + /mcp + cua-win JSON RPC)
│  ├─ tool_cli.py            ← cua-win CLI / HTTP server launcher
│  ├─ tool_rpc.py            ← Shared tool schema / result JSON serialization
│  ├─ server_context.py      ← Per-instance recording / learning / observation
│  │                          registry / UIA gate state
│  ├─ handlers/              ← Platform primitives (1:1 OS API wrappers)
│  │  ├─ base.py             ← Abstract interfaces (ABCs)
│  │  ├─ factory.py          ← HandlerFactory + Handlers dataclass
│  │  ├─ windows.py          ← Windows: ImageGrab + win32gui + pynput
│  │  ├─ windows_uia.py      ← UIA COM tree walker (DFS, direct STA calls)
│  │  ├─ windows_msaa.py     ← MSAA / IAccessible fallback for classic controls
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
│  │  ├─ protocol_service.py       ← 0.2 envelope / errors / artifacts / actions
│  │  ├─ observation_context.py    ← observation/screenshot/zoom freshness
│  │  ├─ action_delta_service.py   ← optional before/after action summaries
│  │  ├─ agent_state_service.py    ← compact agent observation text
│  │  ├─ coordinate_service.py     ← coords / zoom / element center (pure)
│  │  ├─ window_state_service.py   ← WindowSnapshotGeometry + UIAWalkGate
│  │  │                              + get_window_state_snapshot + create_zoom_image
│  │  ├─ window_identity_service.py ← build/resolve_live_window (3-level)
│  │  ├─ replay_resolver.py        ← coord rewrite + stale HWND rewrite
│  │  ├─ capture_service.py        ← virtual-desktop mss capture
│  │  ├─ ocr_service.py            ← lazy OCRProcessor singleton
│  │  ├─ target_profile_service.py ← window-family classification + delivery risk
│  │  ├─ permission_service.py     ← open/command/CDP permission policy
│  │  ├─ validation_service.py     ← argument and path validation
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
│  ├─ mcp_test_one.py        ← Single-tool test script
│  ├─ wgc_smoke.py           ← WGC occlusion / minimized / DPI smoke tests
│  ├─ agent_smoke_fixture.py ← Fixed Windows GUI fixture
│  └─ run_agent_smoke.py     ← Agent smoke prompt / transcript checker
├─ pyproject.toml
└─ README.md
```

**Layer direction**: `mcp_server → services → handlers → OS APIs`. `handlers` never imports `services`. `recording`, `learning`, and `overlay` share capture / OCR / UIA-timeout behaviour through `services` rather than duplicating it.

### Installation

Download releases from
[GitHub Releases](https://github.com/zhangweiooy/cua-pywin/releases). Normal
Windows users should use the installer:

```powershell
.\cua-win-0.4.0-win64-setup.exe
cua-win doctor
```

The installer places the full standalone runtime under
`%LocalAppData%\Programs\cua-win` and does not require administrator
privileges. It can optionally add `cua-win.exe` to the current user's PATH.
The bundled models are installed with the app; users do not download model
files separately.

The release wheel also contains the OmniParser V2 YOLO and
RapidOCR models, and is intended for Python environments or developer
validation:

```powershell
pip install .\cua_pywin-0.4.0-py3-none-any.whl
cua-win doctor
```

The wheel is approximately 660 MiB and is not distributed through normal PyPI.
Wheel installation can still download Python dependencies from the package index.
See [`RELEASE.md`](RELEASE.md) for the developer release process.

For source development:

```bash
cd D:\Documents\Code\cua-pywin
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

Or with uv:

```bash
uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

The default OCR backend is RapidOCR + ONNXRuntime with PaddleOCR PP-OCRv4
mobile models. The det/cls/rec ONNX files and Chinese character dictionary are
bundled, so first initialization does not require network access. Subsequent
daemon calls reuse loaded state.
OCR text nodes preserve `confidence`. Default denoising only drops empty text
and applies deterministic misread fixes; deletion-style filtering for isolated
symbols and single characters requires `CUA_OCR_DENOISE=strict`.

### Quick Start

```bash
# MCP stdio mode (Claude Code / Claude Desktop)
cua-win mcp

# Print copy-paste-safe MCP client config / install commands
cua-win mcp-config
cua-win mcp-config --client claude
cua-win mcp-config --client codex

# Unified HTTP server (binds 127.0.0.1 by default; serves /status + /mcp + token JSON RPC)
cua-win serve --port 8000

# CLI daemon/client (auto-starts a local HTTP daemon by default; no manual serve needed)
cua-win doctor
cua-win status
cua-win list-tools
cua-win computer_get_screen_size
@'
{"window":{"id":5678,"pid":1234},"format":"png","max_image_dimension":1280}
'@ | cua-win call computer_screenshot - --output window.png
@'
{"window":{"id":5678,"pid":1234}}
'@ | cua-win call computer_get_window_state -
cua-win computer_press_key '{\"key\":\"Control_L+s\"}'
cua-win computer_mouse '{\"action\":\"move\",\"x\":100,\"y\":100}'
cua-win computer_recording '{action:status}'
cua-win computer_learn '{action:status}'
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
path that skips SOM/RapidOCR warm-up. Desktop-control and vision calls
should still reuse the daemon.
`cua-win <tool> '<json>'` is rewritten to `cua-win call <tool> '<json>'`.
For Chinese text or deeply nested arguments, prefer UTF-8 stdin
(`'<json>' | cua-win call <tool> -`) or a UTF-8 file
(`cua-win call <tool> @arguments.json`) to avoid PowerShell/native argument
escaping and console code-page transformations. Inline JSON remains supported.
Flat primitive arguments also accept a narrow shorthand such as
`{action:status}`.

`cua-win batch` reads an action array or a
`{"defaults":...,"actions":[...]}` object from stdin, inline JSON, or a UTF-8
JSON file and executes it sequentially through the same daemon process. Entries
may be `{"tool":"computer_click","arguments":{...}}` or
`{"type":"wait","milliseconds":200}`. Execution stops on the first failure by
default; `--continue-on-error` processes the remaining entries. Failures and
Ctrl+C interruptions trigger a best-effort `computer_release_all_inputs` call.
Batch calls suppress the agent cursor trajectory overlay so dense action
sequences do not wait on visual animation; the actual drag `duration` remains
unchanged. Use `cua-win batch --dry-run` to validate and summarize the expanded
actions without starting the daemon or sending input.

Actions that support `capture_after` return a post-action observation in
`data.capture_after`. Pass
`include_post_action_delta=true` only when a lightweight before/after comparison
is needed; it adds one pre-action window observation and returns
`data.post_action_delta` with node-count, added/removed node, text-change,
tree-quality, and before/after `screenshot_id` summaries.

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
version, dependency availability, bundled model files, state directory,
interactive desktop, window enumeration, full-screen screenshot availability,
and existing daemon reachability. It does not start the daemon or send input;
pass `--no-desktop` to skip desktop probes.
For real-app diagnostics, run `uv run python scripts/windows_robustness_smoke.py`.
It prints read-only JSON summaries for Notepad, Edge/Chrome, Windows Terminal,
WeChat, and DingTalk without screenshots or node text.

Use `cua-win serve --no-cli-rpc` when you only want `/status + /mcp`.

### Claude Code Integration

Prefer generating the config or install command instead of hand-writing
PowerShell JSON:

```powershell
cua-win mcp-config
cua-win mcp-config --client claude
```

Equivalent `.mcp.json` shape:

```json
{
  "mcpServers": {
    "cua-win": {
      "command": "cua-win",
      "args": ["mcp"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### Codex Skill

This repository includes a Codex skill at `skills/cua-win`. Copy or link that
directory to `$CODEX_HOME/skills/cua-win` (or
`%USERPROFILE%\.codex\skills\cua-win` when `CODEX_HOME` is unset) to invoke the
workflow guidance in Codex with `$cua-win`.

### MCP Tools

**Screen & Mouse** — `computer_screenshot` `computer_click` `computer_perform_secondary_action` `computer_mouse` `computer_drag` `computer_scroll` `computer_release_all_inputs` `computer_get_screen_size` `computer_get_cursor_position` `computer_get_window_state` `computer_zoom`

**Keyboard** — `computer_type` `computer_press_key` `computer_set_value`

**Windows & Apps** — `computer_open_url` `computer_launch_app` `computer_get_window` `computer_manage_window` `computer_list_windows` `computer_list_apps`

**Desktop & Browser** — `computer_get_desktop_environment` `computer_page`

**Recording & Replay** — `computer_recording` `computer_replay_trajectory`

**Learning** — `computer_learn`

### Protocol 0.2

All 25 tools return one fixed envelope for success and expected business
failures:

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

Tool-specific results live only in `data`. Images live only in MCP image
content and are bound through `artifacts[].content_index`; structured results
do not contain base64 images or debug output paths. Side-effect calls return a
transient `action_id` and a `completed/rejected/unknown` outcome. Unknown
outcomes, stale bindings, and postcondition failures require re-observation,
not blind retries. Argument errors, target rejection, and unavailable providers
are normal MCP results with `success=false` and a stable `error.code`; only
uncaught or transport failures use MCP `isError`.

Daemon `/call` returns
`{protocol_version,result,content,transport_error}`. Normal CLI output and
`--json` both print the `result` envelope. Use
`cua-win call TOOL ... --output PATH` for the primary image or
`--output-dir DIR` for all image artifacts.

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

Protocol 0.2 public windows are strictly `{"id": HWND, "pid": PID}`.
`window_id`, `windowId`, additional window metadata, and top-level target
`pid/window_id` are not accepted.
`computer_list_windows`, `computer_list_apps`, `computer_get_window`, and
`computer_get_window_state` return reusable `window` objects. Use
`computer_get_window({"id":...,"app":...})` to rehydrate a current window after
stale handles, lost bindings, or context resets. Use
`computer_get_window({"active":true})` for the current foreground window.
`window_ref` is internal recording/replay identity metadata and should not be
hand-authored.
`computer_type` and `computer_press_key` also accept canonical `window`.
Targeted text, hotkey, key-press, and key-down calls activate and verify that
window before sending input. Calls without `window` preserve
current-focus behavior. Key-up skips activation so globally held input can
always be released.
`computer_click`, `computer_drag`, and `computer_scroll` also activate and
verify the target when canonical `window` is explicitly supplied. Activation
failure prevents mouse input. On Windows, activation restores a minimized
target, temporarily attaches the relevant input threads, and verifies the
foreground window without injecting a synthetic Alt keystroke.
Action results add `data.action_path.target_profile`,
`data.action_path.delivery_risk`, and `data.action_path.delivery_note` to
explain Chromium, terminal, WPF/WinUI, VCL/SAL, or generic target risk. These
fields are diagnostic only and do not change default input behavior.

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

Teaching start and stop require the same non-empty `session_id`. Status supports
incremental strict-timeline reads through `after_sequence` and `timeline_limit`
(1–200, default 64), with the next cursor in `data.next_sequence`. Stop returns
`data.sessionId`, `data.cutoffSequence`, strict `data.actions[]`, and a bounded
`data.summary` containing only `durationSeconds`, `actionCount`,
`observationCount`, and `degradedObservationCount`. Repeated or concurrent stop
calls must carry the originating session id and return that session's same
completed result. `computer_learn` does not accept `excluded_window_ids`.

```powershell
$sessionId = "teaching-123"
@"
{"action":"start","session_id":"$sessionId","task_description":"demonstrate file export"}
"@ | cua-win call computer_learn -

cua-win computer_learn '{"action":"status","after_sequence":0,"timeline_limit":64}'

@"
{"action":"stop","session_id":"$sessionId"}
"@ | cua-win call computer_learn -
```

Use `cua-win help computer_learn` or `cua-win computer_learn --help` for
tool-specific arguments. Use `cua-win describe computer_learn --no-daemon` for
the machine-readable schema.

Double-click with `computer_click({"count":2,...})`; there is no separate
double-click tool.
Recording start requires a new or empty `output_dir`; cua-win never overwrites
an existing recording or mixes old turns into a new 0.2 trajectory.
Use `computer_click` for a node's primary action and
`computer_perform_secondary_action` for an explicitly exposed non-primary
action. The current secondary action is `action="expand"` for MSAA
dropdown/splitbutton nodes. Window activation remains
`computer_manage_window({"action":"activate",...})`.

`computer_get_window_state` requires a canonical `window` and returns
`data.observation_id`, `data.screenshot_id`, and the matching image artifact.
Element-index actions require the latest `observation_id` for that window.
`window_resized/window_full_res` coordinates and `computer_zoom` require the
latest `screenshot_id`; zoom-coordinate actions require the returned
`zoom_id`. A new observation for the same window immediately stales its older
observation, screenshot, and zoom bindings. Direct screen coordinates,
untargeted low-level mouse calls, current-focus type/key calls, and explicit
canonical-window type/key/manage calls do not require an observation.
Returned `nodes` use `nodes_coordinate_space="window_resized"`, so their
bounding boxes and centers align directly with the target screenshot. By
default only the 100 highest-priority nodes are returned to keep large apps
from producing oversized payloads. Pass `max_nodes=0` to return every node, or
combine `query` and `max_nodes` to filter first and then adjust the cap. This
limit only affects the returned payload; the internal `ElementCache` still
stores complete window-full-resolution coordinates.
`tree_markdown` is omitted by default; request it only for full-tree debugging
with `include_tree_markdown=true`. `response_mode="compact"` caps the image at
960 pixels, returned nodes at 30, and the normal summary at 300 characters,
while omitting verbose capture/provider/vision diagnostics. Public
window-state floats are rounded to two decimal places; internal observation and
coordinate geometry retain their original precision.
Nodes include low-level `actions` and agent-facing `action_hints` such as
`click`, `type`, `set_value`, and `expand`, making actionable/editable/expandable
candidates easier to distinguish. `data.accessibility_hint` and
`data.observation_recommendation` describe whether semantic UIA/MSAA data is
sufficient, whether vision/OCR was needed, and whether callers should narrow
with `query` or raise `max_nodes`.
`data.state_text` is the single compact agent-facing observation text;
`data.agent_state` retains truncation and sampling metadata without duplicating
the text. The summary is capped by `agent_text_limit=500` by default; use
`show_full_text=true` or raise `agent_text_limit` only when the
compact state is insufficient, then inspect `nodes`; explicitly enable
`include_tree_markdown=true` only when the full debug tree is needed.
The public payload keeps canonical node counts; exact duplicate counts and
empty diagnostic fields are omitted.
Target and transient capture descriptors live only in the envelope's
`artifacts[]`. Actual images are MCP image content; each artifact identifies its
role, MIME type, dimensions, byte count, `content_index`, `window`,
`observation_id`, and `screenshot_id`.

### Key Design Decisions

**Per-window screenshot capture** — `PrintWindow` fails on hardware-accelerated windows. `computer_screenshot({"window":{"id":...,"pid":...}})` and `get_window_state` share the Windows.Graphics.Capture-first pixel path with an `ImageGrab.grab()` + `GetWindowRect` + PIL `crop()` fallback. The former returns only target-window pixels without UIA/MSAA/SOM and does not create `observation_id` / `screenshot_id`; use `get_window_state` when follow-up node or window-relative coordinate actions need fresh bindings. `computer_zoom` follows the last observation's capture backend: WGC captures use WGC for native zoom crops and fall back to screen crop only when WGC fails. Multi-monitor negative coordinates and partially-offscreen windows are handled by `WindowSnapshotGeometry`, which explicitly separates the real window origin from the visible crop origin. `capture.capture_metadata.stage_timings` reports window-bounds, WGC attempt, fallback activation, screen capture, decode, crop/resize, and total capture latency. Successful WGC captures additionally expose item, D3D, session setup, frame wait, and texture readback timings. `capture.capture_metadata.capture_reliability`, `degraded_reason`, and `provider_errors` explain WGC fallback, minimized windows, screen-crop occlusion risk, or provider failures.

**UIA/MSAA integration + gates** — COM is called directly inside its STA thread (the previous design submitted each property call to a shared executor and leaked threads when the UIA provider hung). `UIAWalkGate` / `MSAAWalkGate` / `UIAActionGate` (owned by `ServerContext`) provide per-window timeout boundaries with `wait_for + shield + done_callback`. On timeout the gate stays held and the key is marked unhealthy, so fresh requests fall through to SOM, skip MSAA, or use pixel/HID fallback instead of stacking more doomed calls.

**Tree quality + SOM fallback** — `rich(>50) / ok(16-50)` skip SOM; `poor(1-15) / empty(0)` enable SOM vision. `uia_quality` and `vision_quality` are reported separately and do not overwrite each other.

**Screenshot understanding and grounded actions** — The main LLM interprets
the full-window screenshot together with UIA/MSAA semantics, OCR text, and YOLO
icon locations. Prefer an existing node's `element_index` with the latest
`observation_id` when acting; its bounding box and center are grounded in the
observation, so small controls do not require guessed coordinates. YOLO icons
remain unnamed location candidates. For an ambiguous small target, request
`computer_zoom` with the parent `screenshot_id` and inspect that region; use
the returned `zoom_id` for zoom-coordinate actions. The runtime performs no
icon-caption inference or per-icon LLM API calls.

Unlabeled icons do not match semantic text queries. Broaden `query` or raise
`max_nodes` to inspect additional candidates. Detection confidence describes
localization evidence, not confidence in an icon's meaning. SOM runs only when
the semantic tree is poor/empty or `force_som=true` is explicitly requested.

See [the current visual runtime and YOLO ONNX verification](docs/yolo-onnx-poc.md)
for model provenance, current evidence, and validation limits.

**Bundled OmniParser V2 pipeline** — The YOLO detector is bundled at
`som/weights/icon_detect/model.onnx` and runs through ONNX Runtime with
NumPy/OpenCV preprocessing and NMS. PyTorch, Torchvision, and Ultralytics are
export-time dependencies only. YOLO and RapidOCR use four intra-op threads and
one inter-op thread with the CPU arena disabled. Only YOLO and RapidOCR models
are shipped; source checkouts need their complete local bundles, and
`cua-win doctor` rejects missing weights and unresolved Git LFS pointers.

**PaddleOCR ONNX backend** — OCR uses RapidOCR + ONNXRuntime with
PaddleOCR PP-OCRv4 mobile models. `CUA_OCR_TIMEOUT_SECONDS`
overrides per-call OCR timeout, and `CUA_RAPIDOCR_DEVICE=cpu` is the supported v1
runtime. The det/cls/rec models and Chinese character dictionary are bundled under
`computer_server/models/rapidocr_ppocrv4/` and passed to RapidOCR explicitly, so
runtime model download is not supported. `cua-win doctor` verifies their SHA-256 hashes
and rejects unresolved Git LFS pointers. `CUA_RAPIDOCR_MODEL_DIR` overrides the
bundle path for developer validation only; user model updates ship with new
`cua-pywin` releases. OCR text results propagate `confidence` / `ocr_confidence` into vision
nodes. Default denoising only drops empty text and applies deterministic misread
fixes; deletion-style filtering for isolated symbols, tiny ASCII artifacts, and
known icon-shape misreads requires `CUA_OCR_DENOISE=strict`. Set
`CUA_OCR_DENOISE=0` to disable denoising.

**Recording vs Learning** — Recording is agent-perspective (MCP tool replay); Learning is human-perspective (pynput + UIA + OCR enrichment). The two share capture / OCR / UIA-timeout / window-identity through `services/` rather than duplicating logic.

**Recording disk writes** — Turn-folder writes go through a dedicated daemon writer thread + bounded queue so they don't compete with the anyio default executor; full queue drops with a counter in `state.dropped_turns`.

**Learning OCR pipeline** — UIA value probe at record time; active-window screenshot on failure. Before any OCR screenshot lands on disk, `_is_sensitive_context` gates it (UIA `IsPassword` + element title/identifier pattern match) so password / sensitive fields are skipped. At stop, OCR produces full `ocr_text` with bbox+confidence. Model decides, no auto-replacement. The short-session semantic timeline adds `target_profile`, `capture_reliability`, `semantic_degraded_reason`, and `delivery_risk` to `observations[]` and `metrics`.

**CDP target tracking** — `computer_page` tracks pages by stable `target_id` (CDP tab id) rather than `tab_index`. `get_page_info` returns the resolved `target_id` so callers can pin subsequent calls to the same tab; retries re-resolve by `target_id`, never drifting to the wrong page when tab order changes.

**Replay window resolution** — `_build_window_ref` stores `version / pid / window_id / origin / process_name / window_title / class_name / size`. At replay time the live window is resolved in this order: (1) original HWND if still valid, (2) first visible window matching `process_name`, (3) substring match on `window_title`. click / drag / scroll also rewrite coordinates against the new origin — not just `activate_window`.

### Known Limitations

- **SOM adds per-frame latency**: synchronous inference runs in a worker thread; warm-up preloads models but does not remove cold-inference or per-frame cost
- **Recording capture still has an in-action cost**: turn writes use a bounded writer thread, but each sampled action's screenshot capture typically adds about 50-200 ms
- **SOM OCR occlusion noise**: WGC window capture avoids most overlapping-window pollution; screen-crop fallback marks `capture.capture_metadata.occlusion_risk=true`
- **Learning IME not supported in terminals**: PowerShell/CMD lack UIA ValuePattern. Falls back to pinyin + OCR
- **Recording coordinates are absolute**: window position changes break replay. `window_ref` recalculates if window is alive; falls back to process-name/title search for stale IDs
- **GBK encoding**: set `PYTHONIOENCODING=utf-8` in subprocess environments.
  For PowerShell pipelines carrying Chinese JSON, also set
  `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)` before piping.

### Development

```bash
# Single-tool test
uv run python scripts/mcp_test_one.py <tool_name> [json_args|-|@file] --timeout 120
uv run python scripts/mcp_test_one.py --list

# Compile check
python -m py_compile computer_server/*.py
python -m py_compile computer_server/handlers/*.py
python -m py_compile computer_server/recording/*.py
python -m py_compile computer_server/learning/*.py

# Controlled WGC scenarios and optional live-app samples
uv run python scripts/wgc_smoke.py
uv run python scripts/wgc_smoke.py --live-only --app chrome --app dingtalk --repeat 3

# Agent smoke scaffolding
uv run python scripts/agent_smoke_fixture.py
uv run python scripts/run_agent_smoke.py --dry-run --scenario fixture-basic
```
